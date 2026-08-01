"""Desktop-facing recommendation bridge with opaque browser media handles."""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, TypeGuard

from zvec_webview.image_registry import ImageRegistry, ImageRegistryError


class RecommendationServiceError(RuntimeError):
    """A safe failure while adapting backend recommendations for a client."""


class RecommendationClient(Protocol):
    def create_recommendations(
        self, viewer_id: str, request_id: str
    ) -> Mapping[str, Any]: ...

    def mark_recommendations_shown(
        self, viewer_id: str, batch_id: str, event_id: str
    ) -> Mapping[str, Any]: ...

    def record_recommendation_action(
        self,
        viewer_id: str,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: str,
        metadata: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]: ...


class RecommendationService:
    """Inject trusted viewers and turn backend paths into opaque media ids."""

    def __init__(
        self,
        client_provider: Callable[[], RecommendationClient],
        image_registry: ImageRegistry,
        config_home: str | Path,
    ) -> None:
        self._client_provider = client_provider
        self._registry = image_registry
        self._config_home = Path(config_home).expanduser().resolve()
        self._viewer_path = self._config_home / "desktop-recommendation-viewer.json"
        self._viewer_lock = threading.RLock()
        self._desktop_viewer: str | None = None

    def desktop_viewer_id(self) -> str:
        """Return a durable viewer id which is never supplied by the browser."""

        with self._viewer_lock:
            if self._desktop_viewer is not None:
                return self._desktop_viewer
            try:
                if self._viewer_path.exists():
                    payload = json.loads(self._viewer_path.read_text(encoding="utf-8"))
                    viewer_id = (
                        payload.get("viewer_id") if isinstance(payload, dict) else None
                    )
                    if _valid_desktop_viewer(viewer_id):
                        self._desktop_viewer = viewer_id
                        return viewer_id
                self._config_home.mkdir(parents=True, exist_ok=True)
                viewer_id = f"desktop-{secrets.token_hex(32)}"
                temporary = self._viewer_path.with_name(
                    f".{self._viewer_path.name}.{secrets.token_hex(8)}.tmp"
                )
                try:
                    with temporary.open("x", encoding="utf-8") as stream:
                        json.dump(
                            {"viewer_id": viewer_id}, stream, separators=(",", ":")
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self._viewer_path)
                finally:
                    temporary.unlink(missing_ok=True)
                self._desktop_viewer = viewer_id
                return viewer_id
            except (OSError, json.JSONDecodeError) as exc:
                raise RecommendationServiceError("无法读取推荐设备标识。") from exc

    def create_recommendations(
        self, request_id: str, *, viewer_id: str | None = None
    ) -> dict[str, Any]:
        viewer = viewer_id or self.desktop_viewer_id()
        response = self._client_provider().create_recommendations(viewer, request_id)
        if not isinstance(response, Mapping):
            raise RecommendationServiceError("推荐服务返回无效数据。")
        return _browser_batch(response, self._registry)

    def mark_recommendations_shown(
        self,
        batch_id: str,
        event_id: str,
        *,
        viewer_id: str | None = None,
    ) -> None:
        viewer = viewer_id or self.desktop_viewer_id()
        self._client_provider().mark_recommendations_shown(viewer, batch_id, event_id)

    def record_recommendation_action(
        self,
        batch_id: str,
        event_id: str,
        item_id: str,
        action: str,
        metadata: Mapping[str, str] | None = None,
        *,
        viewer_id: str | None = None,
    ) -> None:
        viewer = viewer_id or self.desktop_viewer_id()
        self._client_provider().record_recommendation_action(
            viewer, batch_id, event_id, item_id, action, metadata
        )


def _browser_batch(raw: Mapping[str, Any], registry: ImageRegistry) -> dict[str, Any]:
    request_id = _text(raw.get("request_id"))
    batch_id = _text(raw.get("batch_id"))
    if not request_id or not batch_id:
        raise RecommendationServiceError("推荐服务没有返回批次标识。")
    items: list[dict[str, Any]] = []
    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    for value in raw_items:
        if not isinstance(value, Mapping):
            continue
        item = _browser_item(value, registry)
        if item is not None:
            items.append(item)
    raw_count = _non_negative_int(raw.get("count"))
    filtered = len(items) < raw_count
    partial_reason = _text(raw.get("partial_reason"))
    return {
        "request_id": request_id,
        "batch_id": batch_id,
        "count": len(items),
        "partial": raw.get("partial") is True or filtered,
        "partial_reason": partial_reason or ("media_unavailable" if filtered else ""),
        "quota_degraded": raw.get("quota_degraded") is True or filtered,
        "history_window": _non_negative_int(raw.get("history_window")),
        "quota": _quota(raw.get("quota")),
        "diversity": _diversity(raw.get("diversity")),
        "items": items,
    }


def _browser_item(
    raw: Mapping[str, Any], registry: ImageRegistry
) -> dict[str, Any] | None:
    item_id = _text(raw.get("item_id"))
    source_path = _text(raw.get("source_path"))
    bucket = _text(raw.get("bucket"))
    if (
        not item_id
        or not source_path
        or bucket
        not in {
            "quality",
            "recent",
            "low_exposure",
            "random",
        }
    ):
        return None
    try:
        media = registry.register(Path(source_path))
    except ImageRegistryError:
        return None
    return {
        "item_id": item_id,
        "media_id": media.image_id,
        "name": _text(raw.get("name")) or media.name,
        "width": _non_negative_int(raw.get("width"), fallback=media.width),
        "height": _non_negative_int(raw.get("height"), fallback=media.height),
        "tags": _strings(raw.get("tags")),
        "library_id": _text(raw.get("library_id")),
        "library_name": _text(raw.get("library_name")),
        "content_type": _text(raw.get("content_type")),
        "size_bytes": _non_negative_int(
            raw.get("size_bytes"), fallback=media.size_bytes
        ),
        "bucket": bucket,
        "thumbnail_url": f"api/image/{media.image_id}?variant=thumbnail",
        "preview_url": f"api/image/{media.image_id}?variant=preview",
    }


def _quota(value: Any) -> dict[str, int]:
    source = value if isinstance(value, Mapping) else {}
    return {
        bucket: _non_negative_int(source.get(bucket))
        for bucket in ("quality", "recent", "low_exposure", "random")
    }


def _diversity(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    return {
        "applied": source.get("applied") is True,
        "reason": _text(source.get("reason")),
        "missing_vectors": _non_negative_int(source.get("missing_vectors")),
        "vector_space": _vector_space(source.get("vector_space")),
    }


def _vector_space(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, Any] = {}
    model = _text(value.get("model"))
    metric = _text(value.get("metric"))
    dimension = _non_negative_int(value.get("dimension"))
    if model:
        output["model"] = model
    if metric:
        output["metric"] = metric
    if dimension:
        output["dimension"] = dimension
    return output


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _non_negative_int(value: Any, *, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return fallback


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _valid_desktop_viewer(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == len("desktop-") + 64
        and value.startswith("desktop-")
        and all(character in "0123456789abcdef" for character in value[8:])
    )


__all__ = [
    "RecommendationClient",
    "RecommendationService",
    "RecommendationServiceError",
]
