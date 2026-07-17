from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from PIL import Image

from image_vector_service.backend_server import BackendJobManager
from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import (
    DashScopeEmbeddingClient,
    EmbeddingResponse,
)
from image_vector_service.model_catalog import (
    ModelConfiguration,
    parse_model_configuration,
)
from image_vector_service.service import ImageVectorService
from image_vector_service.vision_tagging_client import (
    TaggingContext,
    VisionTaggingResponse,
    VisionUsage,
)

CUSTOM_EMBEDDING = "custom-vl-embedding-v1"
CUSTOM_PRIMARY = "custom-vl-vision-fast"
CUSTOM_ESCALATION = "custom-vl-vision-quality"


def _custom_model_configuration() -> ModelConfiguration:
    return parse_model_configuration(
        {
            "schema_version": 1,
            "provider": "aliyun_dashscope",
            "models": [
                {
                    "id": CUSTOM_EMBEDDING,
                    "display_name": "Custom embedding",
                    "roles": ["embedding"],
                    "protocol": "dashscope_multimodal_embedding",
                    "dimension": 1024,
                    "enabled": True,
                },
                {
                    "id": CUSTOM_PRIMARY,
                    "display_name": "Custom fast vision",
                    "roles": ["auto_tag_primary"],
                    "protocol": "dashscope_multimodal_conversation",
                    "enabled": True,
                    "pricing": {
                        "input_yuan_per_million": 0.2,
                        "output_yuan_per_million": 2.0,
                        "effective_from": "2026-07-16",
                    },
                },
                {
                    "id": CUSTOM_ESCALATION,
                    "display_name": "Custom quality vision",
                    "roles": ["auto_tag_escalation"],
                    "protocol": "dashscope_multimodal_conversation",
                    "enabled": True,
                    "pricing": {
                        "input_yuan_per_million": 1.0,
                        "output_yuan_per_million": 10.0,
                        "effective_from": "2026-07-16",
                    },
                },
            ],
            "roles": {
                "embedding": CUSTOM_EMBEDDING,
                "auto_tag_primary": CUSTOM_PRIMARY,
                "auto_tag_escalation": CUSTOM_ESCALATION,
            },
        }
    )


class _FakeEmbeddingClient:
    def __init__(self) -> None:
        self.request_count = 0

    def embed_images(self, paths: list[Path]) -> EmbeddingResponse:
        self.request_count += 1
        return EmbeddingResponse(
            vectors=[[1.0, *([0.0] * 1023)] for _path in paths],
            request_id=f"custom-image-{self.request_count}",
            usage={"images": len(paths)},
        )

    def embed_text(self, text: str) -> EmbeddingResponse:
        self.request_count += 1
        return EmbeddingResponse(
            vectors=[[1.0, *([0.0] * 1023)]],
            request_id=f"custom-text-{self.request_count}",
            usage={"text": text},
        )


class _RecordingVisionClient:
    models: list[str] = []

    def __init__(self, config: Any, *, budget_tracker: Any = None) -> None:
        self.config = config
        self.budget_tracker = budget_tracker
        self.request_count = 0

    def tag_image(
        self,
        _path: Path,
        *,
        context: TaggingContext,
    ) -> VisionTaggingResponse:
        del context
        self.request_count += 1
        type(self).models.append(self.config.model)
        usage = VisionUsage(
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            raw={"input_tokens": 100, "output_tokens": 50},
        )
        if self.budget_tracker is not None:
            self.budget_tracker.authorize_next_image()
            self.budget_tracker.record(usage)
        annotation = {
            "schema_version": 2,
            "prompt_version": "people-cosplay-v2",
            "description": "custom model",
            "fields": {},
            "categories": {},
            "entities": {},
            "controlled_tags": [],
            "suggested_tags": [],
            "requires_review": False,
            "review_reasons": [],
            "plus_recommended": self.config.model == CUSTOM_PRIMARY,
            "warnings": [],
        }
        pricing = self.config.pricing
        return VisionTaggingResponse(
            annotation=cast(Any, annotation),
            request_id=f"custom-vision-{self.request_count}",
            usage=usage,
            cost_yuan=(
                pricing.cost(usage.input_tokens, usage.output_tokens)
                if pricing is not None
                else None
            ),
        )


class _BackendRecordingService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def estimate_auto_tags(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {
            "model": kwargs["model"],
            "flash_requests": 1,
            "plus_requests": 0,
        }

    def close(self) -> None:
        return


class DynamicModelIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zvec-dynamic-model-test-"))
        self.models = _custom_model_configuration()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_custom_embedding_reaches_request_and_collection_identity(self) -> None:
        config = ServiceConfig(
            workspace=self.root / "embedding-workspace",
            model_configuration=self.models,
        )
        client = DashScopeEmbeddingClient(config)
        api_response = {
            "output": {
                "embeddings": [{"index": 0, "embedding": [1.0, *([0.0] * 1023)]}]
            },
            "request_id": "custom-embedding-request",
            "usage": {"total_tokens": 12},
        }
        with patch.object(client, "_post_json", return_value=api_response) as post:
            response = client.embed_text("portrait")

        payload = post.call_args.args[0]
        self.assertEqual(payload["model"], CUSTOM_EMBEDDING)
        self.assertEqual(payload["parameters"]["dimension"], 1024)
        self.assertEqual(response.request_id, "custom-embedding-request")

        image_root = self.root / "images"
        image_root.mkdir()
        Image.new("RGB", (16, 16), (120, 80, 200)).save(image_root / "one.png")
        service = ImageVectorService(
            config=config,
            embedding_client=_FakeEmbeddingClient(),
        )
        try:
            report = service.index_folder(str(image_root))
            self.assertEqual(report.inserted, 1)
            metadata = json.loads(
                config.collection_meta_path.read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["model"], CUSTOM_EMBEDDING)
            document = next(iter(service.state.list_entries()))
            cache_key = service._cache_key("text", "portrait")
            expected_cache_key = hashlib.sha256(
                "\0".join(
                    (
                        service.repository.collection_uuid,
                        CUSTOM_EMBEDDING,
                        "1024",
                        "text",
                        "portrait",
                    )
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(cache_key, expected_cache_key)
            stored = service.repository.collection.fetch(
                str(document["doc_id"]),
                output_fields=["model"],
                include_vector=False,
            )[str(document["doc_id"])]
            self.assertEqual(stored.fields["model"], CUSTOM_EMBEDDING)
        finally:
            service.close()

    def test_custom_visual_model_reaches_cost_cache_and_backend_job(self) -> None:
        workspace = self.root / "visual-workspace"
        image_root = self.root / "visual-images"
        image_root.mkdir()
        Image.new("RGB", (18, 18), (40, 140, 220)).save(image_root / "one.png")
        config = ServiceConfig(
            workspace=workspace,
            model_configuration=self.models,
        )
        service = ImageVectorService(
            config=config,
            embedding_client=_FakeEmbeddingClient(),
        )
        _RecordingVisionClient.models = []
        try:
            indexed = service.index_folder(str(image_root))
            self.assertEqual(indexed.inserted, 1)
            estimate = service.estimate_auto_tags(
                scope="latest_index_run",
                max_images=10,
            )
            self.assertEqual(estimate["primary_model"], CUSTOM_PRIMARY)
            self.assertEqual(estimate["escalation_model"], CUSTOM_ESCALATION)
            self.assertEqual(estimate["flash_requests"], 1)
            self.assertEqual(estimate["plus_requests"], 0)

            with patch(
                "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
                _RecordingVisionClient,
            ):
                first = service.auto_tag_images(
                    scope="latest_index_run",
                    max_images=10,
                    max_budget_cny=1.0,
                    external_processing_confirmed=True,
                )
                second = service.auto_tag_images(
                    scope="all",
                    max_images=10,
                    max_budget_cny=1.0,
                    external_processing_confirmed=True,
                )

            self.assertEqual(
                _RecordingVisionClient.models,
                [CUSTOM_PRIMARY, CUSTOM_ESCALATION],
            )
            self.assertEqual(first["primary_model"], CUSTOM_PRIMARY)
            self.assertEqual(first["escalation_model"], CUSTOM_ESCALATION)
            self.assertEqual(first["flash_requests"], 1)
            self.assertEqual(first["plus_requests"], 1)
            self.assertAlmostEqual(first["actual_cost_cny"], 0.00072)
            proposal = first["proposals"][0]
            self.assertEqual(
                proposal["model_trace"],
                [CUSTOM_PRIMARY, CUSTOM_ESCALATION],
            )
            self.assertEqual(proposal["resolved_model"], CUSTOM_ESCALATION)
            cached_models = []
            for model_step in proposal["policy"]["model_steps"]:
                cached = service.auto_tag_cache.get(model_step["cache_key"])
                self.assertIsNotNone(cached)
                assert cached is not None
                cached_models.append(cached["model"])
            self.assertEqual(
                cached_models,
                [CUSTOM_PRIMARY, CUSTOM_ESCALATION],
            )
            self.assertEqual(second["api_request_count"], 0)
            self.assertEqual(second["flash_requests"], 0)
            self.assertEqual(second["plus_requests"], 0)
            self.assertEqual(second["cached"], 1)
        finally:
            service.close()

        backend_services: list[_BackendRecordingService] = []

        def factory(_progress: Any, _cancel: Any) -> _BackendRecordingService:
            backend_service = _BackendRecordingService()
            backend_services.append(backend_service)
            return backend_service

        manager = BackendJobManager(
            instance_id="dynamic-model-test",
            config_fingerprint="dynamic-model-fingerprint",
            config=config,
            service_factory=factory,
        )
        manager.start()
        try:
            submitted = manager.submit({"command": "auto_tag_estimate", "params": {}})
            self.assertEqual(submitted["params"]["model"], CUSTOM_PRIMARY)
            deadline = time.monotonic() + 5
            completed = manager.get(submitted["id"])
            while completed["status"] not in {
                "succeeded",
                "partial",
                "needs_attention",
                "failed",
                "cancelled",
            }:
                if time.monotonic() >= deadline:
                    self.fail("dynamic model backend job did not complete")
                time.sleep(0.01)
                completed = manager.get(submitted["id"])
            self.assertEqual(completed["status"], "succeeded", completed)
            self.assertEqual(completed["result"]["model"], CUSTOM_PRIMARY)
            self.assertEqual(backend_services[0].calls[0]["model"], CUSTOM_PRIMARY)
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
