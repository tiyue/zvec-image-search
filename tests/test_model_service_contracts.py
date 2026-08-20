from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from image_vector_service.config import ServiceConfig
from image_vector_service.model_catalog import (
    CONVERSATION_PROTOCOL,
    default_model_configuration,
)
from image_vector_service.model_services.contracts import (
    EmbeddingProvider,
    EmbeddingResponse,
    ModelInputError,
    ModelProviderError,
    provider_diagnostic_snapshot,
    sanitized_usage,
)
from image_vector_service.model_services.factory import create_model_provider_factory
from image_vector_service.model_services.tagging import VisionTaggingConfig


class _EmbeddingProbe:
    def embed_images(self, _image_paths: list[Path]) -> EmbeddingResponse:
        return EmbeddingResponse([], "", {})

    def embed_text(self, _text: str) -> EmbeddingResponse:
        return EmbeddingResponse([[0.0]], "request", {"total_tokens": 1})


class ModelServiceContractTests(unittest.TestCase):
    def test_embedding_contract_is_structural(self) -> None:
        self.assertIsInstance(_EmbeddingProbe(), EmbeddingProvider)

    def test_response_and_error_attempts_are_strict(self) -> None:
        self.assertEqual(EmbeddingResponse([], "", {}).attempts, 1)
        with self.assertRaisesRegex(ValueError, "attempts"):
            EmbeddingResponse([], "", {}, attempts=-1)
        with self.assertRaisesRegex(ValueError, "attempts"):
            EmbeddingResponse([], "", {}, attempts=1.5)  # type: ignore[arg-type]
        error = ModelProviderError(
            "rate limited",
            category="rate_limit",
            status_code=429,
            code="Throttled",
            attempts=3,
        )
        self.assertEqual(error.category, "rate_limit")
        self.assertEqual(error.attempts, 3)
        with self.assertRaisesRegex(ValueError, "attempts"):
            ModelProviderError("bad attempts", attempts=True)

    def test_input_error_is_splittable_and_pre_request(self) -> None:
        error = ModelInputError("bad image")
        self.assertEqual(error.category, "input")
        self.assertTrue(error.splittable)
        self.assertEqual(error.attempts, 0)

    def test_usage_copy_rejects_nested_arbitrary_objects(self) -> None:
        marker = object()
        self.assertEqual(
            sanitized_usage(
                {
                    "total_tokens": 4,
                    "labels": ["a", marker, ["nested"], None, "b"],
                    "unsafe": marker,
                }
            ),
            {"total_tokens": 4, "labels": ["a", None, "b"]},
        )

    def test_optional_diagnostics_do_not_expose_adapter_attributes(self) -> None:
        class Probe:
            def diagnostic_snapshot(self) -> dict[str, object]:
                return {"attempts": 2, "unsafe": object()}

        self.assertEqual(provider_diagnostic_snapshot(Probe()), {"attempts": 2})
        self.assertIsNone(provider_diagnostic_snapshot(object()))

    def test_factory_omits_absent_optional_adapter_arguments(self) -> None:
        class EmbeddingAdapter:
            def __init__(self, config: ServiceConfig) -> None:
                self.config = config

        class VisionAdapter:
            def __init__(self, config: VisionTaggingConfig) -> None:
                self.config = config

        config = ServiceConfig()
        factory = create_model_provider_factory(config)
        vision_config = VisionTaggingConfig(model=config.auto_tag_primary_model)
        with (
            patch(
                "image_vector_service.model_services.aliyun.embedding."
                "AliyunEmbeddingProvider",
                EmbeddingAdapter,
            ),
            patch(
                "image_vector_service.model_services.aliyun.vision."
                "AliyunVisionTaggingProvider",
                VisionAdapter,
            ),
        ):
            embedding = factory.create_embedding()
            vision = factory.create_vision_tagger(vision_config)

        self.assertIs(embedding.config, config)  # type: ignore[attr-defined]
        self.assertIs(vision.config, vision_config)  # type: ignore[attr-defined]

    def test_factory_rejects_unknown_provider_and_protocol_mismatch(self) -> None:
        model_configuration = default_model_configuration()
        unsupported = replace(model_configuration, provider="other")
        with self.assertRaisesRegex(ValueError, "Unsupported model provider"):
            create_model_provider_factory(
                ServiceConfig(model_configuration=unsupported)
            )

        embedding = model_configuration.model_for_role("embedding")
        mismatched_embedding = replace(
            embedding,
            protocol=CONVERSATION_PROTOCOL,
        )
        mismatched_models = tuple(
            mismatched_embedding if model.model_id == embedding.model_id else model
            for model in model_configuration.models
        )
        mismatched = replace(model_configuration, models=mismatched_models)
        factory = create_model_provider_factory(
            ServiceConfig(model_configuration=mismatched)
        )
        with self.assertRaisesRegex(ValueError, "expected"):
            factory.create_embedding()

    def test_legacy_dashscope_imports_alias_new_adapters_for_one_cycle(self) -> None:
        from image_vector_service.dashscope_client import DashScopeEmbeddingClient
        from image_vector_service.model_services.aliyun.embedding import (
            AliyunEmbeddingProvider,
        )
        from image_vector_service.model_services.aliyun.vision import (
            AliyunVisionTaggingProvider,
        )
        from image_vector_service.model_services.tagging import TaggingContext
        from image_vector_service.vision_tagging_client import (
            DashScopeVisionTaggingClient,
        )
        from image_vector_service.vision_tagging_client import (
            TaggingContext as LegacyTaggingContext,
        )

        self.assertIs(DashScopeEmbeddingClient, AliyunEmbeddingProvider)
        self.assertIs(DashScopeVisionTaggingClient, AliyunVisionTaggingProvider)
        self.assertIs(LegacyTaggingContext, TaggingContext)

    def test_business_modules_do_not_import_or_inspect_concrete_adapters(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        paths = (
            repository / "image_service.py",
            repository / "image_vector_service" / "service.py",
            repository / "image_vector_service" / "annotation_service.py",
            repository / "image_vector_service" / "metadata_backfill.py",
        )
        forbidden_modules = (
            "dashscope_client",
            "vision_tagging_client",
            "model_services.aliyun",
        )
        for path in paths:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported_modules = [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ]
            leaked_attributes = [
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute)
                and node.attr in {"limiter", "request_count"}
            ]
            with self.subTest(path=path.name):
                self.assertFalse(
                    any(
                        forbidden in module
                        for module in imported_modules
                        for forbidden in forbidden_modules
                    )
                )
                self.assertEqual(leaked_attributes, [])


if __name__ == "__main__":
    unittest.main()
