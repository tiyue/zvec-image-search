"""Compatibility imports for the pre-provider-boundary visual tagging module."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .model_services.tagging import (
    EntityAnnotation,
    StableFieldAnnotation,
    TaggingBudget,
    TaggingBudgetExceeded,
    TaggingBudgetTracker,
    TaggingContext,
    VisionAnnotation,
    VisionConfigurationError,
    VisionInputError,
    VisionPricing,
    VisionResponseError,
    VisionSourceChangedError,
    VisionTaggingConfig,
    VisionTaggingError,
    VisionTaggingResponse,
    VisionTransportError,
    VisionUsage,
    parse_annotation_json,
    parse_model_annotation_json,
    sanitize_generated_tag,
)

if TYPE_CHECKING:
    from .model_services.aliyun.vision import AliyunVisionTaggingProvider

    DashScopeVisionTaggingClient = AliyunVisionTaggingProvider


def __getattr__(name: str) -> Any:
    if name == "DashScopeVisionTaggingClient":
        from .model_services.aliyun.vision import AliyunVisionTaggingProvider

        globals()[name] = AliyunVisionTaggingProvider
        return AliyunVisionTaggingProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DashScopeVisionTaggingClient",
    "EntityAnnotation",
    "StableFieldAnnotation",
    "TaggingBudget",
    "TaggingBudgetExceeded",
    "TaggingBudgetTracker",
    "TaggingContext",
    "VisionAnnotation",
    "VisionConfigurationError",
    "VisionInputError",
    "VisionPricing",
    "VisionResponseError",
    "VisionSourceChangedError",
    "VisionTaggingConfig",
    "VisionTaggingError",
    "VisionTaggingResponse",
    "VisionTransportError",
    "VisionUsage",
    "parse_annotation_json",
    "parse_model_annotation_json",
    "sanitize_generated_tag",
]
