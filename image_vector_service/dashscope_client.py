"""Compatibility exports for the Alibaba Cloud embedding adapter."""

from .model_services.aliyun.embedding import (
    AliyunEmbeddingProvider,
    AliyunModelError,
    ImageInputError,
)
from .model_services.contracts import EmbeddingResponse

DashScopeEmbeddingClient = AliyunEmbeddingProvider
DashScopeError = AliyunModelError

__all__ = [
    "DashScopeEmbeddingClient",
    "DashScopeError",
    "EmbeddingResponse",
    "ImageInputError",
]
