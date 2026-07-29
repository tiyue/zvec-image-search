from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

ProviderErrorCategory = Literal[
    "configuration",
    "authentication",
    "input",
    "content_policy",
    "rate_limit",
    "transient",
    "invalid_response",
    "cancelled",
    "unknown",
]


@dataclass(frozen=True)
class EmbeddingResponse:
    """Provider-neutral embedding output consumed by indexing and search."""

    vectors: list[list[float]]
    request_id: str
    usage: dict[str, Any]
    attempts: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or self.attempts < 0
        ):
            raise ValueError("attempts must be a non-negative integer")


class ModelProviderError(RuntimeError):
    """Stable error boundary between model adapters and application services."""

    def __init__(
        self,
        message: str,
        *,
        category: ProviderErrorCategory = "unknown",
        status_code: int | None = None,
        code: str = "",
        splittable: bool = False,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ValueError("attempts must be a non-negative integer")
        self.category = category
        self.status_code = status_code
        self.code = code
        self.splittable = splittable
        self.attempts = attempts


class ModelInputError(ModelProviderError, ValueError):
    """Local input cannot be represented safely for the configured model."""

    def __init__(self, message: str, *, splittable: bool = True) -> None:
        super().__init__(
            message,
            category="input",
            splittable=splittable,
            attempts=0,
        )


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Minimum embedding surface required by application services."""

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse: ...

    def embed_text(self, text: str) -> EmbeddingResponse: ...


TaggingContextT = TypeVar("TaggingContextT", contravariant=True)
TaggingResponseT = TypeVar("TaggingResponseT", covariant=True)


@runtime_checkable
class VisionTaggingProvider(Protocol[TaggingContextT, TaggingResponseT]):
    """Minimum visual-tagging surface required by the coordinator."""

    def tag_image(
        self,
        image_path: Path,
        *,
        context: TaggingContextT | None = None,
    ) -> TaggingResponseT: ...


@runtime_checkable
class ModelProviderDiagnostics(Protocol):
    """Optional provider-neutral diagnostics exposed to maintenance reports."""

    def diagnostic_snapshot(self) -> Mapping[str, object]: ...


def provider_diagnostic_snapshot(value: object) -> dict[str, object] | None:
    """Read optional diagnostics without exposing an adapter's internals."""

    if not isinstance(value, ModelProviderDiagnostics):
        return None
    return sanitized_usage(value.diagnostic_snapshot())


def sanitized_usage(value: Mapping[str, object] | None) -> dict[str, object]:
    """Copy bounded scalar usage metadata without accepting arbitrary objects."""

    if value is None:
        return {}
    result: dict[str, object] = {}
    for key, item in value.items():
        normalized_key = str(key)[:80]
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[normalized_key] = item
        elif isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            result[normalized_key] = [
                entry
                for entry in item[:100]
                if isinstance(entry, (str, int, float, bool)) or entry is None
            ]
    return result
