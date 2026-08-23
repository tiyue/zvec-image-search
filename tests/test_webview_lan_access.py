from __future__ import annotations

import hashlib
import io
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from PIL import Image

from zvec_lan import (
    LanBackendError,
    MemoryCredentialStore,
    SearchPending,
    SearchRequest,
)
from zvec_webview.image_registry import ImageRegistry
from zvec_webview.lan_access import (
    LanAccessController,
    LanAccessError,
    PreviewLanAdapter,
)
from zvec_webview.lan_settings import LanAccessSettings, LanSettingsStore


class _Facade:
    def __init__(self, registry: ImageRegistry, image_id: str) -> None:
        self.image_registry = registry
        self.image_id = image_id
        self.submissions: list[dict[str, Any]] = []
        self.searches: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []

    def bootstrap(self) -> dict[str, Any]:
        return {
            "libraries": [
                {
                    "id": "lib-a",
                    "name": "人物图库",
                    "enabled": True,
                    "image_root": r"C:\must-not-leak",
                },
                {"id": "disabled", "name": "关闭图库", "enabled": False},
            ]
        }

    def submit_lan_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.submissions.append(dict(payload))
        search_id = f"search-{len(self.submissions)}"
        self.searches[search_id] = {
            "id": search_id,
            "status": "queued",
            "page": 1,
            "page_size": 15,
            "total_items": 0,
            "items": [],
        }
        return self.searches[search_id]

    def search(
        self,
        operation_id: str,
        *,
        page: int = 1,
        page_size: int = 15,
    ) -> dict[str, Any]:
        value = dict(self.searches[operation_id])
        value["page"] = page
        value["page_size"] = page_size
        return value

    def cancel_search(self, operation_id: str) -> dict[str, Any]:
        self.cancelled.append(operation_id)
        self.searches[operation_id]["status"] = "cancelled"
        return dict(self.searches[operation_id])

    def complete(self, search_id: str, sha256: str) -> None:
        self.searches[search_id] = {
            "id": search_id,
            "status": "succeeded",
            "page": 1,
            "page_size": 15,
            "total_items": 1,
            "items": [
                {
                    "id": self.image_id,
                    "name": "raiden.jpg",
                    "library_id": "lib-a",
                    "library_name": "人物图库",
                    "sha256": sha256,
                    "confidence": 0.91,
                    "raw_score": 0.72,
                    "width": 80,
                    "height": 120,
                    "size_bytes": 1024,
                    "tags": ["原神", "雷电将军"],
                }
            ],
        }


class _RecommendationBackend:
    def __init__(self, media_id: str) -> None:
        self.media_id = media_id
        self.calls: list[tuple[object, ...]] = []

    def create_recommendations(
        self, request_id: str, *, viewer_id: str | None = None
    ) -> dict[str, object]:
        self.calls.append(("create", viewer_id, request_id))
        return {
            "request_id": request_id,
            "batch_id": "batch-1",
            "count": 1,
            "partial": False,
            "partial_reason": "",
            "quota_degraded": False,
            "items": [
                {
                    "item_id": "item-1",
                    "media_id": self.media_id,
                    "content_type": "image/png",
                    "source_path": r"C:\must-not-leak\raiden.png",
                    "vector": [0.2, 0.4],
                }
            ],
        }

    def create_watch_recommendations(
        self, request_id: str, *, viewer_id: str | None = None
    ) -> dict[str, object]:
        response = self.create_recommendations(request_id, viewer_id=viewer_id)
        self.calls[-1] = ("watch-create", viewer_id, request_id)
        return response

    def mark_recommendations_shown(
        self, batch_id: str, event_id: str, *, viewer_id: str | None = None
    ) -> None:
        self.calls.append(("shown", viewer_id, batch_id, event_id))

    def record_recommendation_action(
        self,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: str,
        metadata: dict[str, str] | None,
        *,
        viewer_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(
            ("action", viewer_id, batch_id, event_id, item_id, action, metadata)
        )
        return {"recorded": False, "preference": "dislike"}


class _Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class PreviewLanAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "raiden.jpg"
        Image.new("RGB", (80, 120), (60, 80, 140)).save(self.source)
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.registry = ImageRegistry(cache_directory=self.root / "cache")
        self.addCleanup(self.registry.close)
        image_id = self.registry.register(self.source).image_id
        self.facade = _Facade(self.registry, image_id)
        self.adapter = PreviewLanAdapter(self.facade)

    def _request(self, *, mode: str = "text") -> SearchRequest:
        return SearchRequest(
            mode=mode,  # type: ignore[arg-type]
            text="原神" if mode in {"text", "tag", "combined"} else None,
            query_image_id=None,
            query_image_path=self.source if mode in {"image", "combined"} else None,
            library_ids=("lib-a",),
            top_k=500,
        )

    def test_watch_recommendations_use_the_dedicated_backend_operation(self) -> None:
        backend = _RecommendationBackend(self.facade.image_id)
        adapter = PreviewLanAdapter(
            self.facade,
            recommendation_backend=backend,
        )  # type: ignore[arg-type]
        adapter.set_recommendation_media_origin("http://192.168.1.20:39000")

        payload = adapter.create_watch_recommendations(
            "watch-request-1",
            client_id="session-a",
            device_id="watch-install-a",
        )

        self.assertEqual(payload["count"], 1)
        self.assertEqual(backend.calls[0][0], "watch-create")

    def test_lists_only_safe_enabled_library_fields(self) -> None:
        libraries = self.adapter.list_libraries(client_id="tablet-a")
        self.assertEqual(
            [(item.id, item.name) for item in libraries],
            [("lib-a", "人物图库")],
        )

    def test_recommendations_use_anonymous_device_viewers_and_media_outlive_searches(
        self,
    ) -> None:
        backend = _RecommendationBackend(self.facade.image_id)
        adapter = PreviewLanAdapter(self.facade, recommendation_backend=backend)  # type: ignore[arg-type]
        adapter.set_recommendation_media_origin("http://192.168.1.20:39000")

        with patch("zvec_webview.lan_access._sha256_file") as digest_file:
            payload = adapter.create_recommendations(
                "request-1", client_id="session-a", device_id="android-install-a"
            )
        digest_file.assert_not_called()
        media_id = payload["items"][0]["media_id"]  # type: ignore[index]
        self.assertNotEqual(media_id, self.facade.image_id)
        self.assertEqual(
            payload["items"][0]["thumbnail_url"],
            f"http://192.168.1.20:39000/api/v1/media/{media_id}/thumbnail",
        )
        self.assertEqual(
            payload["items"][0]["preview_url"],
            f"http://192.168.1.20:39000/api/v1/media/{media_id}/original",
        )
        self.assertNotIn("source_path", str(payload))
        self.assertNotIn("vector", str(payload))
        thumbnail = adapter.resolve_thumbnail(media_id, client_id="session-a")
        self.assertIsNotNone(thumbnail)
        assert thumbnail is not None
        self.assertNotEqual(thumbnail.content, self.source.read_bytes())
        with Image.open(io.BytesIO(thumbnail.content)) as rendered:
            self.assertLessEqual(max(rendered.size), 640)
        self.assertIsNone(adapter.resolve_thumbnail(media_id, client_id="session-b"))

        with patch(
            "zvec_webview.lan_access._sha256_file",
            wraps=lambda path: hashlib.sha256(path.read_bytes()).hexdigest(),
        ) as digest_file:
            resolved = adapter.resolve_original(media_id, client_id="session-a")
            repeated = adapter.resolve_original(media_id, client_id="session-a")
        self.assertEqual(digest_file.call_count, 1)
        self.assertEqual(resolved.path, self.source.resolve())
        self.assertEqual(resolved.sha256, self.digest)
        self.assertEqual(repeated, resolved)

        adapter.mark_recommendations_shown(
            "batch-1",
            "shown-1",
            client_id="session-a",
            device_id="android-install-a",
        )
        action_status = adapter.record_recommendation_action(
            "batch-1",
            "action-1",
            "item-1",
            "like",
            None,
            client_id="session-a",
            device_id="android-install-a",
        )
        viewer = backend.calls[0][1]
        self.assertIsInstance(viewer, str)
        self.assertNotIn("android-install-a", str(viewer))
        self.assertEqual(backend.calls[1][1], viewer)
        self.assertEqual(backend.calls[2][1], viewer)
        self.assertEqual(
            action_status,
            {"recorded": False, "preference": "dislike"},
        )
        adapter.delete_client_session(client_id="session-a")
        self.assertIsNone(adapter.resolve_original(media_id, client_id="session-a"))
        self.assertIsNone(adapter.resolve_thumbnail(media_id, client_id="session-a"))

    def test_recommendation_original_digest_is_lazy_single_flight(self) -> None:
        backend = _RecommendationBackend(self.facade.image_id)
        adapter = PreviewLanAdapter(self.facade, recommendation_backend=backend)  # type: ignore[arg-type]
        adapter.set_recommendation_media_origin("http://192.168.1.20:39000")
        started = threading.Event()
        release = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def counted_digest(path: Path) -> str:
            nonlocal calls
            with calls_lock:
                calls += 1
            started.set()
            release.wait(timeout=5)
            return hashlib.sha256(path.read_bytes()).hexdigest()

        with patch("zvec_webview.lan_access._sha256_file", side_effect=counted_digest):
            payload = adapter.create_recommendations(
                "request-1",
                client_id="session-a",
                device_id="android-install-a",
            )
            self.assertEqual(calls, 0)
            media_id = payload["items"][0]["media_id"]  # type: ignore[index]
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = tuple(
                    executor.submit(
                        adapter.resolve_original,
                        media_id,
                        client_id="session-a",
                    )
                    for _index in range(8)
                )
                self.assertTrue(started.wait(timeout=2))
                release.set()
                resolved = tuple(future.result(timeout=5) for future in futures)

        self.assertEqual(calls, 1)
        self.assertTrue(all(item is not None for item in resolved))
        self.assertTrue(all(item.sha256 == self.digest for item in resolved if item))

    def test_recommendation_source_change_revokes_old_thumbnail_and_original(
        self,
    ) -> None:
        backend = _RecommendationBackend(self.facade.image_id)
        adapter = PreviewLanAdapter(self.facade, recommendation_backend=backend)  # type: ignore[arg-type]
        adapter.set_recommendation_media_origin("http://192.168.1.20:39000")
        first = adapter.create_recommendations(
            "request-1", client_id="session-a", device_id="android-install-a"
        )
        first_media = first["items"][0]["media_id"]  # type: ignore[index]
        self.assertIsNotNone(
            adapter.resolve_thumbnail(first_media, client_id="session-a")
        )

        Image.new("RGB", (96, 128), (140, 60, 80)).save(self.source)
        self.facade.image_id = self.registry.register(self.source).image_id
        backend.media_id = self.facade.image_id
        second = adapter.create_recommendations(
            "request-2", client_id="session-a", device_id="android-install-a"
        )
        second_media = second["items"][0]["media_id"]  # type: ignore[index]

        self.assertNotEqual(first_media, second_media)
        self.assertIsNone(adapter.resolve_thumbnail(first_media, client_id="session-a"))
        self.assertIsNone(adapter.resolve_original(first_media, client_id="session-a"))
        self.assertIsNotNone(
            adapter.resolve_thumbnail(second_media, client_id="session-a")
        )
        self.assertIsNotNone(
            adapter.resolve_original(second_media, client_id="session-a")
        )

    def test_recommendation_media_filter_marks_batch_partial(self) -> None:
        adapter = PreviewLanAdapter(
            self.facade,
            recommendation_backend=_RecommendationBackend("missing-media"),
        )  # type: ignore[arg-type]
        adapter.set_recommendation_media_origin("http://192.168.1.20:39000")

        payload = adapter.create_recommendations(
            "request-1", client_id="session-a", device_id="android-install-a"
        )

        self.assertEqual(payload["count"], 0)
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["partial_reason"], "media_unavailable")
        self.assertTrue(payload["quota_degraded"])

    def test_tag_search_uses_existing_fuzzy_tag_contract_and_is_async(self) -> None:
        search_id = self.adapter.create_search(
            self._request(mode="tag"),
            client_id="tablet-a",
        )

        self.assertEqual(
            self.facade.submissions[0],
            {
                "mode": "tag",
                "library_ids": ["lib-a"],
                "top_k": 500,
                "page_size": 100,
                "sort_mode": "confidence",
                "text": "原神",
                "tags": ["原神"],
                "tag_mode": "any",
            },
        )
        pending = self.adapter.get_search_page(
            search_id,
            page=1,
            page_size=100,
            client_id="tablet-a",
        )
        self.assertIsInstance(pending, SearchPending)
        self.assertEqual(pending.status, "queued")  # type: ignore[union-attr]

    def test_media_capabilities_are_per_search_per_client_and_revocable(self) -> None:
        first = self.adapter.create_search(self._request(), client_id="tablet-a")
        second = self.adapter.create_search(self._request(), client_id="tablet-b")
        self.facade.complete(first, self.digest)
        self.facade.complete(second, self.digest)

        first_page = self.adapter.get_search_page(
            first, page=1, page_size=15, client_id="tablet-a"
        )
        second_page = self.adapter.get_search_page(
            second, page=1, page_size=15, client_id="tablet-b"
        )
        assert not isinstance(first_page, SearchPending)
        assert not isinstance(second_page, SearchPending)
        first_media = first_page.items[0].media_id
        second_media = second_page.items[0].media_id

        self.assertNotEqual(first_media, self.facade.image_id)
        self.assertNotEqual(first_media, second_media)
        source = self.adapter.resolve_original(first_media, client_id="tablet-a")
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source.path, self.source.resolve())
        self.assertEqual(source.sha256, self.digest)
        self.assertIsNone(
            self.adapter.resolve_original(first_media, client_id="tablet-b")
        )

        self.adapter.delete_search(first, client_id="tablet-a")
        self.assertIsNone(
            self.adapter.resolve_original(first_media, client_id="tablet-a")
        )
        self.assertIsNotNone(
            self.adapter.resolve_original(second_media, client_id="tablet-b")
        )

        third = self.adapter.create_search(self._request(), client_id="tablet-a")
        self.facade.complete(third, self.digest)
        third_page = self.adapter.get_search_page(
            third, page=1, page_size=15, client_id="tablet-a"
        )
        assert not isinstance(third_page, SearchPending)
        third_media = third_page.items[0].media_id
        self.adapter.delete_client_session(client_id="tablet-a")
        self.assertIsNone(
            self.adapter.resolve_original(third_media, client_id="tablet-a")
        )
        self.assertIsNotNone(
            self.adapter.resolve_original(second_media, client_id="tablet-b")
        )

    def test_source_change_mints_new_capability_and_invalidates_old_etag(self) -> None:
        search_id = self.adapter.create_search(self._request(), client_id="tablet-a")
        self.facade.complete(search_id, self.digest)
        first_page = self.adapter.get_search_page(
            search_id, page=1, page_size=15, client_id="tablet-a"
        )
        assert not isinstance(first_page, SearchPending)
        first_media = first_page.items[0].media_id

        Image.new("RGB", (80, 120), (140, 60, 80)).save(self.source)
        changed_digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.facade.complete(search_id, changed_digest)
        second_page = self.adapter.get_search_page(
            search_id, page=1, page_size=15, client_id="tablet-a"
        )
        assert not isinstance(second_page, SearchPending)
        second_media = second_page.items[0].media_id

        self.assertNotEqual(first_media, second_media)
        self.assertIsNone(
            self.adapter.resolve_original(first_media, client_id="tablet-a")
        )
        updated = self.adapter.resolve_original(second_media, client_id="tablet-a")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.sha256, changed_digest)

    def test_indexed_digest_verification_is_single_flight_and_cross_search_cached(
        self,
    ) -> None:
        first = self.adapter.create_search(self._request(), client_id="tablet-a")
        second = self.adapter.create_search(self._request(), client_id="tablet-b")
        self.facade.complete(first, self.digest)
        self.facade.complete(second, self.digest)
        first_page = self.adapter.get_search_page(
            first, page=1, page_size=15, client_id="tablet-a"
        )
        second_page = self.adapter.get_search_page(
            second, page=1, page_size=15, client_id="tablet-b"
        )
        assert not isinstance(first_page, SearchPending)
        assert not isinstance(second_page, SearchPending)
        capabilities = (
            (first_page.items[0].media_id, "tablet-a"),
            (second_page.items[0].media_id, "tablet-b"),
        )
        call_count = 0
        count_lock = threading.Lock()

        def counted_digest(path: Path) -> str:
            nonlocal call_count
            with count_lock:
                call_count += 1
            # Keep the first owner in flight long enough for every worker to
            # contend on the same verification key.
            time.sleep(0.05)
            return hashlib.sha256(path.read_bytes()).hexdigest()

        with patch("zvec_webview.lan_access._sha256_file", side_effect=counted_digest):
            with ThreadPoolExecutor(max_workers=8) as executor:
                resolved = tuple(
                    executor.map(
                        lambda index: self.adapter.resolve_original(
                            capabilities[index % len(capabilities)][0],
                            client_id=capabilities[index % len(capabilities)][1],
                        ),
                        range(8),
                    )
                )
            self.assertTrue(all(item is not None for item in resolved))
            self.assertEqual(call_count, 1)

            third = self.adapter.create_search(self._request(), client_id="tablet-c")
            self.facade.complete(third, self.digest)
            third_page = self.adapter.get_search_page(
                third, page=1, page_size=15, client_id="tablet-c"
            )
            assert not isinstance(third_page, SearchPending)
            self.assertIsNotNone(
                self.adapter.resolve_original(
                    third_page.items[0].media_id,
                    client_id="tablet-c",
                )
            )
            self.assertEqual(call_count, 1)

    def test_digest_verification_cache_invalidates_when_file_version_changes(
        self,
    ) -> None:
        first = self.adapter.create_search(self._request(), client_id="tablet-a")
        self.facade.complete(first, self.digest)
        first_page = self.adapter.get_search_page(
            first, page=1, page_size=15, client_id="tablet-a"
        )
        assert not isinstance(first_page, SearchPending)
        call_count = 0

        def counted_digest(path: Path) -> str:
            nonlocal call_count
            call_count += 1
            return hashlib.sha256(path.read_bytes()).hexdigest()

        with patch("zvec_webview.lan_access._sha256_file", side_effect=counted_digest):
            self.assertIsNotNone(
                self.adapter.resolve_original(
                    first_page.items[0].media_id,
                    client_id="tablet-a",
                )
            )
            self.assertEqual(call_count, 1)

            Image.new("RGB", (80, 120), (20, 180, 90)).save(self.source)
            changed_digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
            second = self.adapter.create_search(self._request(), client_id="tablet-a")
            self.facade.complete(second, changed_digest)
            second_page = self.adapter.get_search_page(
                second, page=1, page_size=15, client_id="tablet-a"
            )
            assert not isinstance(second_page, SearchPending)
            self.assertIsNotNone(
                self.adapter.resolve_original(
                    second_page.items[0].media_id,
                    client_id="tablet-a",
                )
            )
            self.assertEqual(call_count, 2)

    def test_new_search_reclaims_previous_search_after_app_restart(self) -> None:
        first = self.adapter.create_search(self._request(), client_id="tablet-a")
        self.facade.complete(first, self.digest)
        first_page = self.adapter.get_search_page(
            first, page=1, page_size=15, client_id="tablet-a"
        )
        assert not isinstance(first_page, SearchPending)
        first_media = first_page.items[0].media_id
        self.facade.searches[first]["status"] = "running"

        second = self.adapter.create_search(self._request(), client_id="tablet-a")

        self.assertNotEqual(first, second)
        self.assertIn(first, self.facade.cancelled)
        self.assertIsNone(
            self.adapter.resolve_original(first_media, client_id="tablet-a")
        )
        with self.assertRaises(LanBackendError):
            self.adapter.get_search_page(
                first, page=1, page_size=15, client_id="tablet-a"
            )

    def test_concurrent_search_creation_leaves_one_search_per_client(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    self.adapter.create_search,
                    self._request(),
                    client_id="tablet-a",
                )
                for _index in range(8)
            ]
            search_ids: list[str] = []
            superseded = 0
            for future in futures:
                try:
                    search_ids.append(future.result())
                except LanBackendError as exc:
                    self.assertEqual(exc.code, "search_superseded")
                    superseded += 1

        active = 0
        for search_id in search_ids:
            try:
                self.adapter.get_search_page(
                    search_id,
                    page=1,
                    page_size=15,
                    client_id="tablet-a",
                )
            except LanBackendError:
                continue
            active += 1
        self.assertEqual(active, 1)
        self.assertEqual(superseded + len(self.facade.cancelled), 7)

    def test_search_ttl_is_refreshed_by_media_access_then_expires_lazily(
        self,
    ) -> None:
        clock = _Clock()
        adapter = PreviewLanAdapter(
            self.facade,
            search_ttl_seconds=10,
            cleanup_interval_seconds=0,
            clock=clock,
        )
        search_id = adapter.create_search(self._request(), client_id="tablet-a")
        self.facade.complete(search_id, self.digest)
        page = adapter.get_search_page(
            search_id, page=1, page_size=15, client_id="tablet-a"
        )
        assert not isinstance(page, SearchPending)
        media_id = page.items[0].media_id

        clock.now += 9
        self.assertIsNotNone(adapter.resolve_original(media_id, client_id="tablet-a"))
        clock.now += 9
        adapter.list_libraries(client_id="tablet-b")
        self.assertIsNotNone(adapter.resolve_original(media_id, client_id="tablet-a"))
        clock.now += 11
        adapter.list_libraries(client_id="tablet-b")
        self.assertIsNone(adapter.resolve_original(media_id, client_id="tablet-a"))

    def test_expired_running_search_is_cancelled(self) -> None:
        clock = _Clock()
        adapter = PreviewLanAdapter(
            self.facade,
            search_ttl_seconds=10,
            cleanup_interval_seconds=0,
            clock=clock,
        )
        search_id = adapter.create_search(self._request(), client_id="tablet-a")
        clock.now += 11

        adapter.list_libraries(client_id="tablet-b")

        self.assertIn(search_id, self.facade.cancelled)


class _Gateway:
    instances: list[_Gateway] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.is_running = False
        self.address = SimpleNamespace(
            origin=f"http://{kwargs['advertised_host']}:{kwargs['api_port']}",
            discovery_port=kwargs["discovery_port"],
        )
        self.stop_error: Exception | None = None
        self.revoke_calls = 0
        self.__class__.instances.append(self)

    def start(self) -> object:
        self.is_running = True
        return self.address

    def stop(self) -> None:
        self.is_running = False
        if self.stop_error is not None:
            raise self.stop_error

    def pending_pairings(self) -> tuple[object, ...]:
        return ()

    def paired_clients(self) -> tuple[object, ...]:
        return ()

    def approve_pairing(self, _pairing_id: str) -> None:
        return None

    def reject_pairing(self, _pairing_id: str) -> None:
        return None

    def revoke_device(self, _device_id: str | None = None) -> bool:
        self.revoke_calls += 1
        return True


class LanAccessControllerTests(unittest.TestCase):
    def test_selected_private_interface_and_stable_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = LanSettingsStore(root)
            settings.save(
                LanAccessSettings(
                    enabled=True,
                    bind_host="192.168.1.20",
                    port=39000,
                    display_name="Zvec tablet",
                )
            )
            registry = ImageRegistry(cache_directory=root / "cache")
            self.addCleanup(registry.close)
            facade = _Facade(registry, "unused")
            _Gateway.instances.clear()
            controller = LanAccessController(
                facade,
                root,
                settings_store=settings,
                gateway_factory=_Gateway,
                credential_store=MemoryCredentialStore(),
            )

            first = controller.start()
            self.assertTrue(first["running"])
            self.assertEqual(first["address"], "http://192.168.1.20:39000")
            gateway = _Gateway.instances[-1]
            self.assertEqual(gateway.kwargs["bind_host"], "192.168.1.20")
            self.assertEqual(gateway.kwargs["advertised_host"], "192.168.1.20")
            self.assertIs(gateway.kwargs["recommendation_backend"], controller._adapter)
            first_identity = gateway.kwargs["instance_id"]

            controller.stop()
            controller.start()
            self.assertEqual(
                _Gateway.instances[-1].kwargs["instance_id"],
                first_identity,
            )
            controller.close()

    def test_failed_stop_retains_server_for_fail_closed_device_revoke(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = LanSettingsStore(root)
            settings.save(
                LanAccessSettings(
                    enabled=True,
                    bind_host="192.168.1.20",
                    port=39000,
                    display_name="Zvec tablet",
                )
            )
            registry = ImageRegistry(cache_directory=root / "cache")
            self.addCleanup(registry.close)
            _Gateway.instances.clear()
            controller = LanAccessController(
                _Facade(registry, "unused"),
                root,
                settings_store=settings,
                gateway_factory=_Gateway,
                credential_store=MemoryCredentialStore(),
            )
            controller.start()
            gateway = _Gateway.instances[-1]
            gateway.stop_error = RuntimeError("cleanup failed")

            with self.assertRaises(LanAccessError):
                controller.stop()
            gateway.stop_error = None
            controller.revoke_device()

            self.assertEqual(gateway.revoke_calls, 1)
            controller.stop()


if __name__ == "__main__":
    unittest.main()
