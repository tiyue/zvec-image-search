from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PIL import Image

from zvec_host.recommendation_service import RecommendationService, _personalization
from zvec_webview.image_registry import ImageRegistry


class _Client:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls: list[tuple[object, ...]] = []

    def create_recommendations(self, viewer_id: str, request_id: str) -> dict[str, Any]:
        self.calls.append(("create", viewer_id, request_id))
        return {
            "request_id": request_id,
            "batch_id": "batch-1",
            "count": 1,
            "partial": False,
            "diversity": {
                "applied": True,
                "vector_space": {
                    "model": "clip-test",
                    "dimension": 1024,
                    "metric": "COSINE",
                    "secret": "must-not-leak",
                },
            },
            "personalization": {
                "applied": True,
                "effective_count": 12,
                "reason": None,
                "viewer_id": "must-not-leak",
            },
            "items": [
                {
                    "item_id": "item-1",
                    "name": "raiden.png",
                    "bucket": "quality",
                    "source_path": str(self.source),
                    "vector": [0.1, 0.2],
                    "width": 80,
                    "height": 120,
                    "preference": "like",
                }
            ],
        }

    def mark_recommendations_shown(
        self, viewer_id: str, batch_id: str, event_id: str
    ) -> dict[str, Any]:
        self.calls.append(("shown", viewer_id, batch_id, event_id))
        return {"recorded": True}

    def record_recommendation_action(
        self,
        viewer_id: str,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: str,
        metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            ("action", viewer_id, batch_id, event_id, item_id, action, metadata)
        )
        return {"recorded": False, "preference": "dislike"}


class RecommendationServiceTests(unittest.TestCase):
    def test_personalization_forwards_only_safe_reason_codes(self) -> None:
        self.assertEqual(
            _personalization(
                {"applied": False, "effective_count": 10, "reason": "replayed"}
            ),
            {"applied": False, "effective_count": 10, "reason": "replayed"},
        )
        self.assertIsNone(
            _personalization(
                {"applied": False, "effective_count": 10, "reason": "private-detail"}
            )["reason"]
        )

    def test_persists_viewer_and_hides_backend_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raiden.png"
            Image.new("RGB", (80, 120), (50, 80, 150)).save(source)
            registry = ImageRegistry(cache_directory=root / "cache")
            self.addCleanup(registry.close)
            client = _Client(source)
            service = RecommendationService(lambda: client, registry, root)

            first = service.create_recommendations("request-1")
            viewer = client.calls[0][1]
            self.assertIsInstance(viewer, str)
            self.assertEqual(first["count"], 1)
            item = first["items"][0]
            self.assertIn("media_id", item)
            self.assertIn(item["media_id"], item["thumbnail_url"])
            self.assertNotIn("source_path", item)
            self.assertNotIn("vector", item)
            self.assertNotIn(str(source), str(first))
            self.assertEqual(item["preference"], "like")
            self.assertEqual(
                first["personalization"],
                {"applied": True, "effective_count": 12, "reason": None},
            )
            self.assertNotIn("viewer_id", str(first["personalization"]))
            self.assertEqual(
                first["diversity"]["vector_space"],
                {"model": "clip-test", "dimension": 1024, "metric": "COSINE"},
            )

            replacement = RecommendationService(lambda: client, registry, root)
            replacement.mark_recommendations_shown("batch-1", "shown-1")
            action = replacement.record_recommendation_action(
                "batch-1", "action-1", "item-1", "like"
            )
            self.assertEqual(client.calls[1][1], viewer)
            self.assertEqual(client.calls[2][1], viewer)
            self.assertEqual(
                action,
                {"recorded": False, "preference": "dislike"},
            )

    def test_desktop_forwards_only_safe_performance_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raiden.png"
            Image.new("RGB", (80, 120), (50, 80, 150)).save(source)
            registry = ImageRegistry(cache_directory=root / "cache")
            self.addCleanup(registry.close)

            class Client(_Client):
                def create_recommendations(
                    self, viewer_id: str, request_id: str
                ) -> dict[str, Any]:
                    response = super().create_recommendations(viewer_id, request_id)
                    response["performance"] = {
                        "candidate_read_ms": 12.5,
                        "candidate_initial_read_ms": 0,
                        "candidate_expansion_read_ms": 2.5,
                        "preference_profile_ms": 3.25,
                        "selection_ms": 4.5,
                        "result_hydration_ms": 5.75,
                        "total_ms": 26.0,
                        "initial_request_count": 2,
                        "expansion_request_count": 0,
                        "profile_cache_hit": False,
                        "path": str(source),
                        "source_path": str(source),
                        "viewer_id": viewer_id,
                        "device_id": "android-secret",
                        "request_id": request_id,
                        "batch_id": "batch-secret",
                        "library_id": "library-secret",
                        "preference": "like",
                        "preferences": ["private"],
                    }
                    return response

            result = RecommendationService(
                lambda: Client(source), registry, root
            ).create_recommendations("request-1")

            self.assertEqual(
                result["performance"],
                {
                    "candidate_read_ms": 12.5,
                    "candidate_initial_read_ms": 0,
                    "candidate_expansion_read_ms": 2.5,
                    "preference_profile_ms": 3.25,
                    "selection_ms": 4.5,
                    "result_hydration_ms": 5.75,
                    "total_ms": 26.0,
                    "initial_request_count": 2,
                    "expansion_request_count": 0,
                    "profile_cache_hit": False,
                },
            )
            self.assertNotIn(str(source), str(result["performance"]))

    def test_desktop_drops_invalid_performance_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raiden.png"
            Image.new("RGB", (80, 120), (50, 80, 150)).save(source)
            registry = ImageRegistry(cache_directory=root / "cache")
            self.addCleanup(registry.close)

            class Client(_Client):
                def create_recommendations(
                    self, viewer_id: str, request_id: str
                ) -> dict[str, Any]:
                    response = super().create_recommendations(viewer_id, request_id)
                    response["performance"] = {
                        "candidate_read_ms": float("nan"),
                        "candidate_initial_read_ms": float("inf"),
                        "candidate_expansion_read_ms": -0.1,
                        "preference_profile_ms": True,
                        "selection_ms": "4.5",
                        "result_hydration_ms": None,
                        "total_ms": float("-inf"),
                        "initial_request_count": True,
                        "expansion_request_count": 1.0,
                        "profile_cache_hit": 1,
                    }
                    return response

            result = RecommendationService(
                lambda: Client(source), registry, root
            ).create_recommendations("request-1")

            self.assertNotIn("performance", result)

    def test_lan_explicit_viewer_never_receives_performance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raiden.png"
            Image.new("RGB", (80, 120), (50, 80, 150)).save(source)
            registry = ImageRegistry(cache_directory=root / "cache")
            self.addCleanup(registry.close)

            class Client(_Client):
                def create_recommendations(
                    self, viewer_id: str, request_id: str
                ) -> dict[str, Any]:
                    response = super().create_recommendations(viewer_id, request_id)
                    response["performance"] = {
                        "candidate_read_ms": 1.0,
                        "initial_request_count": 1,
                        "profile_cache_hit": True,
                    }
                    return response

            client = Client(source)
            lan_viewer = f"lan-{'a' * 64}"
            result = RecommendationService(
                lambda: client, registry, root
            ).create_recommendations(
                "request-1",
                viewer_id=lan_viewer,
            )

            self.assertNotIn("performance", result)
            self.assertEqual(
                client.calls[0],
                ("create", lan_viewer, "request-1"),
            )

    def test_concurrent_first_viewer_read_uses_one_persisted_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raiden.png"
            Image.new("RGB", (80, 120), (50, 80, 150)).save(source)
            registry = ImageRegistry(cache_directory=root / "cache")
            self.addCleanup(registry.close)
            service = RecommendationService(lambda: _Client(source), registry, root)

            with ThreadPoolExecutor(max_workers=16) as executor:
                viewers = list(
                    executor.map(lambda _index: service.desktop_viewer_id(), range(64))
                )

            self.assertEqual(len(set(viewers)), 1)
            persisted = json.loads(
                (root / "desktop-recommendation-viewer.json").read_text()
            )
            self.assertEqual(persisted["viewer_id"], viewers[0])

    def test_filtered_media_marks_the_batch_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = ImageRegistry(cache_directory=root / "cache")
            self.addCleanup(registry.close)

            class Client(_Client):
                def create_recommendations(
                    self, viewer_id: str, request_id: str
                ) -> dict[str, Any]:
                    response = super().create_recommendations(viewer_id, request_id)
                    response["items"][0]["source_path"] = str(root / "missing.png")
                    return response

            result = RecommendationService(
                lambda: Client(root / "missing.png"), registry, root
            ).create_recommendations("request-1")
            self.assertTrue(result["partial"])
            self.assertEqual(result["partial_reason"], "media_unavailable")
            self.assertTrue(result["quota_degraded"])


if __name__ == "__main__":
    unittest.main()
