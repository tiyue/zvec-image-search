from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Protocol

from ..model_catalog import (
    CONVERSATION_PROTOCOL,
    EMBEDDING_PROTOCOL,
    MODEL_PROVIDER,
    ModelDefinition,
)
from .contracts import EmbeddingProvider, VisionTaggingProvider

if TYPE_CHECKING:
    from ..config import ServiceConfig
    from ..rate_limiter import ModelRateLimiter
    from .tagging import (
        TaggingBudgetTracker,
        TaggingContext,
        VisionTaggingConfig,
        VisionTaggingResponse,
    )


class ModelProviderFactory(Protocol):
    """Assembly boundary; application services never import concrete adapters."""

    def create_embedding(
        self,
        *,
        limiter: ModelRateLimiter | None = None,
        cancel_event: threading.Event | None = None,
    ) -> EmbeddingProvider: ...

    def create_vision_tagger(
        self,
        config: VisionTaggingConfig,
        *,
        budget_tracker: TaggingBudgetTracker | None = None,
        limiter: ModelRateLimiter | None = None,
        cancel_event: threading.Event | None = None,
    ) -> VisionTaggingProvider[TaggingContext, VisionTaggingResponse]: ...


class _AliyunModelProviderFactory:
    def __init__(self, config: ServiceConfig) -> None:
        self._config = config

    def create_embedding(
        self,
        *,
        limiter: ModelRateLimiter | None = None,
        cancel_event: threading.Event | None = None,
    ) -> EmbeddingProvider:
        from .aliyun.embedding import AliyunEmbeddingProvider

        definition = self._config.model_configuration.model_for_role("embedding")
        _require_protocol(definition, EMBEDDING_PROTOCOL)
        options: dict[str, object] = {}
        if limiter is not None:
            options["limiter"] = limiter
        if cancel_event is not None:
            options["cancel_event"] = cancel_event
        adapter_type: Any = AliyunEmbeddingProvider
        return adapter_type(self._config, **options)

    def create_vision_tagger(
        self,
        config: VisionTaggingConfig,
        *,
        budget_tracker: TaggingBudgetTracker | None = None,
        limiter: ModelRateLimiter | None = None,
        cancel_event: threading.Event | None = None,
    ) -> VisionTaggingProvider[TaggingContext, VisionTaggingResponse]:
        from .aliyun.vision import AliyunVisionTaggingProvider

        definition = self._config.model_configuration.by_id.get(config.model)
        if definition is None:
            raise ValueError(f"Unknown visual model: {config.model}")
        _require_protocol(definition, CONVERSATION_PROTOCOL)
        options: dict[str, object] = {}
        if budget_tracker is not None:
            options["budget_tracker"] = budget_tracker
        if limiter is not None:
            options["limiter"] = limiter
        if cancel_event is not None:
            options["cancel_event"] = cancel_event
        adapter_type: Any = AliyunVisionTaggingProvider
        return adapter_type(config, **options)


def create_model_provider_factory(config: ServiceConfig) -> ModelProviderFactory:
    provider = config.model_configuration.provider
    if provider != MODEL_PROVIDER:
        raise ValueError(f"Unsupported model provider: {provider}")
    return _AliyunModelProviderFactory(config)


def _require_protocol(definition: ModelDefinition, expected: str) -> None:
    if definition.protocol != expected:
        raise ValueError(
            f"Model {definition.model_id!r} uses {definition.protocol!r}; "
            f"expected {expected!r}."
        )
