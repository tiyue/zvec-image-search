"""Public data contracts for the Android LAN gateway.

The gateway deliberately exchanges typed, path-free values with adapters.  A
future PreviewFacade adapter can translate desktop objects into these records
without giving the LAN HTTP layer access to settings or native actions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

SearchMode = Literal["text", "tag", "image", "combined"]
SearchProgress = Literal["queued", "running"]
RecommendationAction = Literal["open", "like", "export", "dislike"]


@dataclass(frozen=True, slots=True)
class LibraryInfo:
    """Safe library metadata exposed to Android."""

    id: str
    name: str
    count: int | None = None


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """Validated LAN search request passed to the desktop adapter."""

    mode: SearchMode
    text: str | None
    query_image_id: str | None
    query_image_path: Path | None
    library_ids: tuple[str, ...]
    top_k: int


@dataclass(frozen=True, slots=True)
class SearchResultItem:
    """One path-free result item returned to the Android viewer."""

    media_id: str
    score: float | None = None
    name: str | None = None
    width: int | None = None
    height: int | None = None
    tags: tuple[str, ...] = ()
    library_id: str | None = None
    library_name: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class SearchPage:
    """A page from a previously submitted search."""

    search_id: str
    page: int
    page_size: int
    total: int
    items: tuple[SearchResultItem, ...]


@dataclass(frozen=True, slots=True)
class SearchPending:
    """A submitted search whose requested result page is not ready yet."""

    search_id: str
    status: SearchProgress
    retry_after_seconds: int = 1


@dataclass(frozen=True, slots=True)
class MediaSource:
    """An indexed original file resolved from an opaque media identifier.

    ``sha256`` is the digest stored by the index, without quotes or a prefix.
    It is used as the strong HTTP ETag.  The path is internal-only and is never
    serialized into a response or an error.
    """

    path: Path
    sha256: str
    content_type: str | None = None


class LanSearchBackend(Protocol):
    """Narrow search callback surface implemented by a desktop adapter."""

    def list_libraries(self, *, client_id: str) -> Sequence[LibraryInfo]:
        """Return only LAN-safe library metadata."""

    def create_search(
        self,
        request: SearchRequest,
        *,
        client_id: str,
    ) -> str:
        """Submit a search and return its opaque identifier."""

    def get_search_page(
        self,
        search_id: str,
        *,
        page: int,
        page_size: int,
        client_id: str,
    ) -> SearchPage | SearchPending:
        """Return a page, or a short-lived asynchronous progress record."""

    def delete_search(self, search_id: str, *, client_id: str) -> None:
        """Cancel or release a temporary search owned by the client."""

    def delete_client_session(self, *, client_id: str) -> None:
        """Release every search/media capability owned by a bearer session."""


class RecommendationBackend(Protocol):
    """Recommendation callbacks independent from search and media lifecycles."""

    def create_recommendations(
        self,
        request_id: str,
        *,
        client_id: str,
        device_id: str,
    ) -> Mapping[str, object]:
        """Create a device-scoped recommendation batch response."""

    def mark_recommendations_shown(
        self,
        batch_id: str,
        event_id: str,
        *,
        client_id: str,
        device_id: str,
    ) -> None:
        """Record a successful display for the supplied batch event."""

    def record_recommendation_action(
        self,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: RecommendationAction,
        metadata: Mapping[str, str] | None,
        *,
        client_id: str,
        device_id: str,
    ) -> None:
        """Record one explicit recommendation interaction."""


class MediaResolver(Protocol):
    """Resolve an opaque media id to an indexed original source."""

    def resolve_original(
        self,
        media_id: str,
        *,
        client_id: str,
    ) -> MediaSource | None:
        """Return the indexed source or ``None`` when it is unavailable."""


class LanBackendError(RuntimeError):
    """A safe, intentional error raised by a LAN adapter.

    Adapters must use a user-facing message and must never include an absolute
    path, credential, image content, or traceback text.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def libraries_payload(libraries: Sequence[LibraryInfo]) -> dict[str, object]:
    """Serialize libraries through a strict allow-list."""

    values: list[dict[str, object]] = []
    for library in libraries:
        item: dict[str, object] = {
            "id": library.id,
            "name": _safe_display_name(library.name),
        }
        if library.count is not None:
            item["count"] = library.count
        values.append(item)
    return {"libraries": values}


def search_page_payload(page: SearchPage) -> dict[str, object]:
    """Serialize a result page without ever accepting arbitrary adapter keys."""

    items: list[dict[str, object]] = []
    for result in page.items:
        item: dict[str, object] = {
            "media_id": result.media_id,
            "tags": list(result.tags),
        }
        optional: tuple[tuple[str, object | None], ...] = (
            ("score", result.score),
            (
                "name",
                None if result.name is None else _safe_display_name(result.name),
            ),
            ("width", result.width),
            ("height", result.height),
            ("library_id", result.library_id),
            (
                "library_name",
                None
                if result.library_name is None
                else _safe_display_name(result.library_name),
            ),
            ("content_type", result.content_type),
            ("size_bytes", result.size_bytes),
        )
        item.update((key, value) for key, value in optional if value is not None)
        items.append(item)
    return {
        "search_id": page.search_id,
        "page": page.page,
        "page_size": page.page_size,
        "total": page.total,
        "items": items,
    }


def _safe_display_name(value: str) -> str:
    leaf = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    leaf = "".join(
        character
        for character in leaf
        if ord(character) >= 32 and ord(character) != 127
    )
    return leaf[:255] or "Unnamed"
