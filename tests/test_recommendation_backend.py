from __future__ import annotations

import http.client
import json
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from image_vector_service.backend_server import (
    _recommendation_candidate,
    _recommendation_exposure_counts,
    _recommendation_final_preferences,
    _recommendation_response,
    create_backend_server,
)
from image_vector_service.config import ServiceConfig
from image_vector_service.library_config import LibraryCatalog, LibraryDefinition
from image_vector_service.recommendation_store import (
    RecommendationBatch,
    RecommendationBatchItem,
    SharedPreference,
)
from zvec_host.backend_api import BackendApiClient


class _RecommendationService:
    def __init__(self, library: LibraryDefinition, source_root: Path) -> None:
        self.library = library
        self.owner_thread = threading.get_ident()
        self.call_threads: list[int] = []
        self.candidate_limits: list[int] = []
        self.preference_vector_reads: list[tuple[str, ...]] = []
        self.drop_first_recommendation_load = False
        self.candidate_vectors_enabled = True
        self.preference_vectors_enabled = True
        self.preference_vector_error = False
        self.respect_candidate_limit = False
        self.vector_model = "space-a" if self.library.library_id == "a" else "space-b"
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
        del random_cursor
        self.call_threads.append(threading.get_ident())
        self.candidate_limits.append(limit_per_pool)
        values = list(self.items.values())
        if self.respect_candidate_limit:
            values = values[:limit_per_pool]
        return {
            "vector_space": {
                "model": self.vector_model,
                "dimension": 2,
                "metric": "COSINE",
            },
            "candidates": [
                value if self.candidate_vectors_enabled else {**value, "vector": None}
                for value in values
            ],
        }

    def load_recommendation_vectors(self, doc_ids: list[str]) -> dict[str, object]:
        self.call_threads.append(threading.get_ident())
        self.preference_vector_reads.append(tuple(doc_ids))
        if self.preference_vector_error:
            raise OSError("preference vectors temporarily unavailable")
        items = []
        if self.preference_vectors_enabled:
            items = [
                {
                    "doc_id": doc_id,
                    "sha256": self.items[doc_id]["sha256"],
                    "vector": self.items[doc_id]["vector"],
                }
                for doc_id in doc_ids
                if doc_id in self.items
            ]
        return {
            "vector_space": {
                "model": self.vector_model,
                "dimension": 2,
                "metric": "COSINE",
            },
            "items": items,
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

    def test_final_preference_reads_are_chunked_below_store_limit(self) -> None:
        class Store:
            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def final_preferences(
                self, sha256_values: tuple[str, ...]
            ) -> dict[str, SharedPreference]:
                self.calls.append(sha256_values)
                return {
                    value: SharedPreference(value, "like", "a", value, 1)
                    for value in sha256_values
                }

        store = Store()
        values = [f"sha-{index}" for index in range(1_001)]

        preferences = _recommendation_final_preferences(store, values)

        self.assertEqual(len(preferences), 1_001)
        self.assertEqual([len(values) for values in store.calls], [800, 201])

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
        slots = ("quality",) * 4 + ("low_exposure",) * 6 + ("random",) * 5
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
            personalization={
                "applied": False,
                "effective_count": 0,
                "reason": "insufficient_preferences",
            },
        )

        self.assertFalse(response["partial"])
        self.assertTrue(response["quota_degraded"])
        self.assertEqual(response["quota"]["quality"], 4)
        self.assertEqual(response["quota"]["random"], 5)
        self.assertNotIn("recent", response["quota"])

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

    def _seed_shown_history(self, viewer_id: str, count: int) -> None:
        sources = [
            (library_id, doc_id, source)
            for library_id, service in self.services.items()
            for doc_id, source in service.items.items()
        ][:count]

        def seed(store: Any) -> None:
            for index, (library_id, doc_id, source) in enumerate(sources):
                batch = store.create_batch(
                    viewer_id,
                    f"history-request-{index}",
                    (
                        RecommendationBatchItem(
                            item_id=f"history-item-{index}",
                            position=0,
                            candidate_id=f"{library_id}:{doc_id}",
                            library_id=library_id,
                            doc_id=doc_id,
                            sha256=str(source["sha256"]),
                            slot="random",
                        ),
                    ),
                )
                store.mark_shown(
                    viewer_id,
                    batch.batch_id,
                    f"history-shown-{index}",
                )

        self.manager._recommendation_call(seed)

    def _preference_read_count(self) -> int:
        return sum(
            len(service.preference_vector_reads) for service in self.services.values()
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
        self.assertEqual(
            created["quota"],
            {"quality": 5, "low_exposure": 6, "random": 4},
        )
        self.assertFalse(created["diversity"]["applied"])
        self.assertEqual(created["diversity"]["reason"], "incompatible_vector_spaces")
        self.assertEqual(
            created["personalization"],
            {
                "applied": False,
                "effective_count": 0,
                "reason": "insufficient_preferences",
            },
        )
        self.assertTrue(all(item["preference"] is None for item in created["items"]))
        self.assertTrue(all("vector" not in item for item in created["items"]))
        self.assertTrue(all("source_path" in item for item in created["items"]))
        performance = created["performance"]
        self.assertEqual(performance["initial_request_count"], 2)
        self.assertEqual(performance["expansion_request_count"], 0)
        self.assertFalse(performance["profile_cache_hit"])
        for key in (
            "candidate_read_ms",
            "candidate_initial_read_ms",
            "candidate_expansion_read_ms",
            "preference_profile_ms",
            "selection_ms",
            "result_hydration_ms",
            "total_ms",
        ):
            self.assertTrue(math.isfinite(performance[key]), key)
            self.assertGreaterEqual(performance[key], 0, key)
        for library_id, service in self.services.items():
            self.assertEqual(service.candidate_limits, [100])
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
        self.assertEqual(repeated["performance"]["initial_request_count"], 0)
        self.assertEqual(repeated["performance"]["expansion_request_count"], 0)
        for service in self.services.values():
            self.assertEqual(service.candidate_limits, [100])

    def test_watch_create_returns_five_without_changing_standard_batch(self) -> None:
        watch_status, watch = self.request(
            "/v1/watch/recommendations",
            {"viewer_id": "watch-viewer", "request_id": "watch-request"},
        )
        standard_status, standard = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "desktop-request"},
        )

        self.assertEqual(watch_status, 200)
        self.assertEqual(watch["count"], 5)
        self.assertFalse(watch["partial"])
        self.assertEqual(
            watch["quota"],
            {"quality": 2, "low_exposure": 2, "random": 1},
        )
        self.assertEqual(standard_status, 200)
        self.assertEqual(standard["count"], 15)
        self.assertEqual(
            standard["quota"],
            {"quality": 5, "low_exposure": 6, "random": 4},
        )

    def test_replays_a_persisted_legacy_recent_item(self) -> None:
        doc_id, source = next(iter(self.services["a"].items.items()))
        legacy_item = RecommendationBatchItem(
            item_id="legacy-item",
            position=0,
            candidate_id=f"a:{doc_id}",
            library_id="a",
            doc_id=doc_id,
            sha256=str(source["sha256"]),
            slot="recent",
        )
        self.manager._recommendation_call(
            lambda store: store.create_batch(
                "legacy-viewer", "legacy-request", (legacy_item,)
            )
        )

        status, replay = self.request(
            "/v1/recommendations",
            {"viewer_id": "legacy-viewer", "request_id": "legacy-request"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(replay["items"][0]["bucket"], "recent")
        self.assertNotIn("recent", replay["quota"])

    def test_history_shortage_expands_and_recovers_the_full_240_window(self) -> None:
        service = self.services["a"]
        source_root = Path(next(iter(service.items.values()))["source_path"]).parent
        for index in range(24, 130):
            doc_id = f"{index:064x}"
            source = source_root / f"a-{index}.jpg"
            source.write_bytes(f"a-{index}".encode())
            service.items[doc_id] = {
                "doc_id": doc_id,
                "sha256": f"{index + 1:064x}",
                "root_id": "root",
                "relative_path": source.name,
                "name": source.name,
                "content_type": "image/jpeg",
                "size_bytes": source.stat().st_size,
                "mtime_ns": index,
                "width": 2_000,
                "height": 1_200,
                "tags": [service.library.name],
                "album_id": f"root\0album-{index // 3}",
                "character": None,
                "vector": (1.0, float(index + 1)),
                "source_path": str(source),
            }
        service.respect_candidate_limit = True
        self.services["b"].items.clear()
        self.services["b"].respect_candidate_limit = True
        self._seed_shown_history("history-viewer", 100)
        preferred_doc_id = f"{129:064x}"

        def seed_preference(store: Any) -> None:
            source = service.items[preferred_doc_id]
            batch = store.create_batch(
                "preference-viewer",
                "expanded-preference-request",
                (
                    RecommendationBatchItem(
                        item_id="expanded-preference-item",
                        position=0,
                        candidate_id=f"a:{preferred_doc_id}",
                        library_id="a",
                        doc_id=preferred_doc_id,
                        sha256=str(source["sha256"]),
                        slot="random",
                    ),
                ),
            )
            store.record_action(
                "preference-viewer",
                batch.batch_id,
                "expanded-preference-like",
                "expanded-preference-item",
                "like",
            )

        self.manager._recommendation_call(seed_preference)

        status, created = self.request(
            "/v1/recommendations",
            {"viewer_id": "history-viewer", "request_id": "history-expanded"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(created["count"], 15)
        self.assertEqual(created["history_window"], 240)
        self.assertNotIn(
            f"a:{preferred_doc_id}",
            {item["media_id"] for item in created["items"]},
        )
        self.assertEqual(created["performance"]["initial_request_count"], 2)
        self.assertEqual(created["performance"]["expansion_request_count"], 2)
        self.assertGreaterEqual(
            created["performance"]["candidate_expansion_read_ms"], 0
        )
        for service in self.services.values():
            self.assertEqual(service.candidate_limits, [100, 256])
        batch = self.manager._recommendation_call(
            lambda store: store.batch_for_request(
                "history-viewer",
                "history-expanded",
            )
        )
        self.assertIsNotNone(batch)
        if batch is not None:
            self.assertEqual(len(batch.items), 15)

    def test_quota_degradation_expands_even_when_the_first_batch_is_complete(
        self,
    ) -> None:
        for service in self.services.values():
            for item in service.items.values():
                item["width"] = 1
                item["height"] = 1

        status, created = self.request(
            "/v1/recommendations",
            {"viewer_id": "quota-viewer", "request_id": "quota-expanded"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(created["count"], 15)
        self.assertTrue(created["quota_degraded"])
        self.assertEqual(created["quota"]["quality"], 0)
        self.assertEqual(created["performance"]["expansion_request_count"], 2)
        for service in self.services.values():
            self.assertEqual(service.candidate_limits, [100, 256])

    def test_partial_candidates_try_only_the_initial_and_expanded_limits(self) -> None:
        for service in self.services.values():
            service.items = dict(list(service.items.items())[:4])

        status, created = self.request(
            "/v1/recommendations",
            {"viewer_id": "partial-viewer", "request_id": "partial-expanded"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(created["partial"])
        self.assertEqual(created["count"], 8)
        self.assertEqual(created["performance"]["expansion_request_count"], 2)
        for service in self.services.values():
            self.assertEqual(service.candidate_limits, [100, 256])

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

    def test_latest_cross_viewer_preference_is_replayed_and_globally_excluded(
        self,
    ) -> None:
        _status, desktop = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "desktop-shared"},
        )
        _status, android = self.request(
            "/v1/recommendations",
            {"viewer_id": "android-viewer", "request_id": "android-shared"},
        )
        desktop_by_media = {item["media_id"]: item for item in desktop["items"]}
        android_by_media = {item["media_id"]: item for item in android["items"]}
        shared_media = next(iter(desktop_by_media.keys() & android_by_media.keys()))

        _status, liked = self.request(
            f"/v1/recommendations/{desktop['batch_id']}/actions",
            {
                "viewer_id": "desktop-viewer",
                "event_id": "desktop-shared-like",
                "item_id": desktop_by_media[shared_media]["item_id"],
                "action": "like",
            },
        )
        self.assertTrue(liked["recorded"])
        self.assertEqual(liked["preference"], "like")
        _status, disliked = self.request(
            f"/v1/recommendations/{android['batch_id']}/actions",
            {
                "viewer_id": "android-viewer",
                "event_id": "android-shared-dislike",
                "item_id": android_by_media[shared_media]["item_id"],
                "action": "dislike",
            },
        )
        self.assertTrue(disliked["recorded"])
        self.assertEqual(disliked["preference"], "dislike")
        _status, stale_like_replay = self.request(
            f"/v1/recommendations/{desktop['batch_id']}/actions",
            {
                "viewer_id": "desktop-viewer",
                "event_id": "desktop-shared-like",
                "item_id": desktop_by_media[shared_media]["item_id"],
                "action": "like",
            },
        )
        self.assertFalse(stale_like_replay["recorded"])
        self.assertEqual(stale_like_replay["preference"], "dislike")

        _status, replay = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "desktop-shared"},
        )
        replay_by_media = {item["media_id"]: item for item in replay["items"]}
        self.assertEqual(replay_by_media[shared_media]["preference"], "dislike")

        _status, fresh = self.request(
            "/v1/recommendations",
            {"viewer_id": "third-viewer", "request_id": "third-shared"},
        )
        self.assertNotIn(shared_media, {item["media_id"] for item in fresh["items"]})

    def test_personalization_cache_reuses_and_precisely_invalidates_profiles(
        self,
    ) -> None:
        for service in self.services.values():
            service.vector_model = "compatible-space"
        _status, feedback_batch = self.request(
            "/v1/recommendations",
            {"viewer_id": "feedback-viewer", "request_id": "feedback-batch"},
        )
        feedback_items = feedback_batch["items"][:10]
        for index, item in enumerate(feedback_items):
            self.request(
                f"/v1/recommendations/{feedback_batch['batch_id']}/actions",
                {
                    "viewer_id": "feedback-viewer",
                    "event_id": f"feedback-{index}",
                    "item_id": item["item_id"],
                    "action": "like" if index % 2 == 0 else "dislike",
                },
            )

        _status, personalized = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "personalized-batch"},
        )

        self.assertEqual(personalized["personalization"]["effective_count"], 10)
        self.assertTrue(personalized["personalization"]["applied"])
        self.assertIsNone(personalized["personalization"]["reason"])
        self.assertFalse(personalized["performance"]["profile_cache_hit"])
        cold_reads = self._preference_read_count()
        self.assertGreater(cold_reads, 0)
        for service in self.services.values():
            self.assertTrue(
                all(
                    thread_id == service.owner_thread
                    for thread_id in service.call_threads
                )
            )

        _status, hot = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "personalized-hot"},
        )
        self.assertTrue(hot["personalization"]["applied"])
        self.assertTrue(hot["performance"]["profile_cache_hit"])
        self.assertEqual(self._preference_read_count(), cold_reads)

        self.request(
            f"/v1/recommendations/{hot['batch_id']}/shown",
            {"viewer_id": "other-viewer", "event_id": "hot-shown"},
        )
        for index, action in enumerate(("open", "export")):
            self.request(
                f"/v1/recommendations/{hot['batch_id']}/actions",
                {
                    "viewer_id": "other-viewer",
                    "event_id": f"hot-{action}",
                    "item_id": hot["items"][index]["item_id"],
                    "action": action,
                },
            )
        _status, after_non_preference = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "after-non-preference"},
        )
        self.assertTrue(after_non_preference["performance"]["profile_cache_hit"])
        self.assertEqual(self._preference_read_count(), cold_reads)

        cache_before_same = self.manager._recommendation_profile_cache
        self.assertIsNotNone(cache_before_same)
        _status, same_like = self.request(
            f"/v1/recommendations/{feedback_batch['batch_id']}/actions",
            {
                "viewer_id": "feedback-viewer",
                "event_id": "feedback-same-like",
                "item_id": feedback_items[0]["item_id"],
                "action": "like",
            },
        )
        self.assertTrue(same_like["recorded"])
        cache_after_same = self.manager._recommendation_profile_cache
        self.assertIsNotNone(cache_after_same)
        if cache_before_same is not None and cache_after_same is not None:
            self.assertGreater(
                cache_after_same.last_preference_sequence,
                cache_before_same.last_preference_sequence,
            )
        _status, after_same = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "after-same-like"},
        )
        self.assertTrue(after_same["performance"]["profile_cache_hit"])
        self.assertEqual(self._preference_read_count(), cold_reads)

        _status, idempotent = self.request(
            f"/v1/recommendations/{feedback_batch['batch_id']}/actions",
            {
                "viewer_id": "feedback-viewer",
                "event_id": "feedback-0",
                "item_id": feedback_items[0]["item_id"],
                "action": "like",
            },
        )
        self.assertFalse(idempotent["recorded"])
        self.assertIsNotNone(self.manager._recommendation_profile_cache)

        self.services["b"].vector_model = "incompatible-space"
        _status, incompatible = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "incompatible-vectors"},
        )
        self.assertFalse(incompatible["personalization"]["applied"])
        self.assertEqual(
            incompatible["personalization"]["reason"],
            "incompatible_vector_spaces",
        )

        self.services["b"].vector_model = "compatible-space"
        for service in self.services.values():
            service.candidate_vectors_enabled = False
        _status, candidate_vectors_missing = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "candidate-vectors-missing"},
        )
        self.assertFalse(candidate_vectors_missing["personalization"]["applied"])
        self.assertEqual(
            candidate_vectors_missing["personalization"]["reason"],
            "vectors_unavailable",
        )

        for service in self.services.values():
            service.candidate_vectors_enabled = True
        _status, changed = self.request(
            f"/v1/recommendations/{feedback_batch['batch_id']}/actions",
            {
                "viewer_id": "feedback-viewer",
                "event_id": "feedback-changed-dislike",
                "item_id": feedback_items[0]["item_id"],
                "action": "dislike",
            },
        )
        self.assertTrue(changed["recorded"])
        self.assertEqual(changed["preference"], "dislike")
        self.assertIsNone(self.manager._recommendation_profile_cache)
        _status, rebuilt = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "rebuilt-profile"},
        )
        self.assertTrue(rebuilt["personalization"]["applied"])
        self.assertFalse(rebuilt["performance"]["profile_cache_hit"])
        rebuilt_reads = self._preference_read_count()
        self.assertGreater(rebuilt_reads, cold_reads)

        for service in self.services.values():
            service.preference_vectors_enabled = False
        self.request(
            f"/v1/recommendations/{feedback_batch['batch_id']}/actions",
            {
                "viewer_id": "feedback-viewer",
                "event_id": "feedback-invalidate-missing",
                "item_id": feedback_items[1]["item_id"],
                "action": "like",
            },
        )
        _status, degraded = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "missing-vectors"},
        )
        self.assertFalse(degraded["personalization"]["applied"])
        self.assertEqual(degraded["personalization"]["effective_count"], 0)
        self.assertEqual(degraded["personalization"]["reason"], "vectors_unavailable")

        for service in self.services.values():
            service.preference_vectors_enabled = True
        self.services["a"].preference_vector_error = True
        self.request(
            f"/v1/recommendations/{feedback_batch['batch_id']}/actions",
            {
                "viewer_id": "feedback-viewer",
                "event_id": "feedback-invalidate-error",
                "item_id": feedback_items[2]["item_id"],
                "action": "dislike",
            },
        )
        status, failed_read = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "failed-vector-read"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(failed_read["count"], 15)
        self.assertFalse(failed_read["personalization"]["applied"])
        self.assertEqual(
            failed_read["personalization"]["reason"],
            "vectors_unavailable",
        )

        self.services["a"].preference_vector_error = False
        reads_before_restart = self._preference_read_count()
        self.manager._close_recommendation_store()
        _status, after_restart = self.request(
            "/v1/recommendations",
            {"viewer_id": "other-viewer", "request_id": "after-cache-restart"},
        )
        self.assertFalse(after_restart["performance"]["profile_cache_hit"])
        self.assertTrue(after_restart["personalization"]["applied"])
        self.assertGreater(self._preference_read_count(), reads_before_restart)

    def test_shown_and_exposure_remain_viewer_isolated(self) -> None:
        _status, desktop = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-isolated", "request_id": "desktop-isolated"},
        )
        self.request(
            f"/v1/recommendations/{desktop['batch_id']}/shown",
            {"viewer_id": "desktop-isolated", "event_id": "shown-isolated"},
        )

        batch = self.manager._recommendation_call(
            lambda store: store.batch_for_request(
                "desktop-isolated", "desktop-isolated"
            )
        )
        self.assertIsNotNone(batch)
        if batch is not None:
            sha256 = batch.items[0].sha256
            counts = self.manager._recommendation_call(
                lambda store: (
                    store.exposure_counts("desktop-isolated", [sha256]),
                    store.exposure_counts("android-isolated", [sha256]),
                )
            )
            self.assertEqual(counts[0][sha256], 1)
            self.assertEqual(counts[1][sha256], 0)

    def test_synchronous_client_calls_recommendation_contract(self) -> None:
        client = BackendApiClient(
            f"http://{self.server.server_address[0]}:{self.server.server_address[1]}",
            self.token,
        )

        created = client.create_recommendations("desktop-viewer", "request-client")
        watch = client.create_watch_recommendations("watch-viewer", "watch-client")
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
        self.assertEqual(watch["count"], 5)

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

    def test_replay_drops_a_doc_whose_sha_changed_after_batch_creation(self) -> None:
        _status, created = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "request-replaced"},
        )
        replaced = created["items"][0]
        self.request(
            f"/v1/recommendations/{created['batch_id']}/actions",
            {
                "viewer_id": "desktop-viewer",
                "event_id": "replaced-like",
                "item_id": replaced["item_id"],
                "action": "like",
            },
        )
        library_id, doc_id = replaced["media_id"].split(":", 1)
        self.services[library_id].items[doc_id]["sha256"] = "f" * 64

        status, replay = self.request(
            "/v1/recommendations",
            {"viewer_id": "desktop-viewer", "request_id": "request-replaced"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(replay["partial"])
        self.assertTrue(replay["quota_degraded"])
        self.assertEqual(sum(replay["quota"].values()), replay["count"])
        self.assertNotIn(
            replaced["media_id"],
            {item["media_id"] for item in replay["items"]},
        )


if __name__ == "__main__":
    unittest.main()
