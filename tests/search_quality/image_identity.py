from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")


class ImageIdentityError(ValueError):
    """Raised when image identity metadata is invalid or contradictory."""


@dataclass(frozen=True)
class ImageReference:
    image_id: str
    sha256: str | None
    context: str


def normalize_sha256(value: Any, context: str) -> str | None:
    """Return a canonical digest, treating only an absent/null value as missing."""
    if value is None:
        return None
    if not isinstance(value, str) or value != value.strip():
        raise ImageIdentityError(f"{context}: sha256 must be 64 hexadecimal characters")
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ImageIdentityError(f"{context}: sha256 must be 64 hexadecimal characters")
    return value.lower()


def parse_image_reference(value: dict[str, Any], context: str) -> ImageReference:
    image_id = value.get("image_id")
    if not isinstance(image_id, str) or not image_id.strip():
        raise ImageIdentityError(f"{context}: image_id must be a non-empty string")
    digest = normalize_sha256(value.get("sha256"), context)
    return ImageReference(image_id=image_id.strip(), sha256=digest, context=context)


class ImageIdentityIndex:
    """Resolve content-global identities while retaining legacy image-id aliases.

    A SHA-256 digest is authoritative when available. If a reference omits it, a
    digest learned for the same image_id is used; otherwise the legacy image_id is
    the identity. Multiple image_ids may intentionally point at one digest (the
    same bytes in different Collections), but one image_id may never claim two
    different digests.
    """

    def __init__(self) -> None:
        self._sha256_by_image_id: dict[str, str] = {}

    def add(self, value: dict[str, Any], context: str) -> ImageReference:
        reference = parse_image_reference(value, context)
        known = self._sha256_by_image_id.get(reference.image_id)
        if (
            known is not None
            and reference.sha256 is not None
            and known != reference.sha256
        ):
            raise ImageIdentityError(
                f"{context}: image_id {reference.image_id!r} has conflicting "
                "sha256 values"
            )
        if reference.sha256 is not None:
            self._sha256_by_image_id[reference.image_id] = reference.sha256
        return reference

    def key(self, reference: ImageReference) -> tuple[str, str]:
        digest = reference.sha256 or self._sha256_by_image_id.get(reference.image_id)
        if digest is not None:
            return ("sha256", digest)
        return ("image_id", reference.image_id)

    def require_unique(
        self,
        references: list[ImageReference],
        context: str,
    ) -> None:
        seen: set[tuple[str, str]] = set()
        for reference in references:
            key = self.key(reference)
            if key in seen:
                kind, value = key
                raise ImageIdentityError(
                    f"{context}: duplicate global image identity ({kind}={value})"
                )
            seen.add(key)


def canonical_image_reference(value: dict[str, Any], context: str) -> dict[str, str]:
    """Validate and return the stable JSON representation used by local datasets."""
    reference = parse_image_reference(value, context)
    result = {"image_id": reference.image_id}
    if reference.sha256 is not None:
        result["sha256"] = reference.sha256
    return result
