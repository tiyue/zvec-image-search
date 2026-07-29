"""Provider-neutral model service contracts and assembly."""

from .contracts import (
    EmbeddingProvider,
    EmbeddingResponse,
    ModelInputError,
    ModelProviderDiagnostics,
    ModelProviderError,
    ProviderErrorCategory,
    VisionTaggingProvider,
    provider_diagnostic_snapshot,
)
from .factory import ModelProviderFactory, create_model_provider_factory

__all__ = [
    "EmbeddingProvider",
    "EmbeddingResponse",
    "ModelInputError",
    "ModelProviderDiagnostics",
    "ModelProviderError",
    "ModelProviderFactory",
    "ProviderErrorCategory",
    "VisionTaggingProvider",
    "create_model_provider_factory",
    "provider_diagnostic_snapshot",
]
