from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from .config import ConfigurationError
from .state import IndexStateReader

_FOLDER_KEY_PREFIX = "zvec-folder-v1."
_MAX_FOLDER_KEY_BYTES = 8_192


class LibraryBrowser:
    """Read-only, non-blocking folder and image catalog for one library."""

    def __init__(self, *, library_id: str, state_path: Path):
        normalized_id = str(library_id).strip()
        if not normalized_id:
            raise ValueError("library_id must be a non-empty string.")
        self.library_id = normalized_id
        self._reader = IndexStateReader(state_path)
        collection_uuid = self._reader.get_metadata("collection_uuid")
        if not collection_uuid:
            self._reader.close()
            raise ConfigurationError("The library state has no collection_uuid.")
        self.collection_uuid = collection_uuid

    def list_folders(
        self,
        *,
        root_id: str | None = None,
        query: str = "",
        offset: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        total, raw_folders = self._reader.list_folders(
            root_id=root_id,
            query=query,
            offset=offset,
            limit=limit,
        )
        folders = []
        for raw in raw_folders:
            relative_folder = str(raw["relative_folder"])
            parent = (
                str(PurePosixPath(relative_folder).parent)
                if relative_folder and str(PurePosixPath(relative_folder).parent) != "."
                else ""
            )
            folders.append(
                {
                    **raw,
                    "folder_key": self._folder_key(
                        str(raw["root_id"]), relative_folder
                    ),
                    "parent_relative_folder": parent,
                    "depth": len(PurePosixPath(relative_folder).parts)
                    if relative_folder
                    else 0,
                }
            )
        roots = []
        for raw in self._reader.list_folder_roots():
            root_id_value = str(raw["root_id"])
            roots.append(
                {
                    **raw,
                    "folder_key": self._folder_key(root_id_value, ""),
                }
            )
        return {
            "library_id": self.library_id,
            "folders": folders,
            "roots": roots,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(folders) < total,
            "undo_available": self._reader.manual_tag_undo_available(),
        }

    def folder_images(
        self,
        folder_key: str,
        *,
        include_subfolders: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        root_id, relative_folder = self._decode_folder_key(folder_key)
        if self._reader.root_path(root_id) is None:
            raise ConfigurationError("The selected folder root no longer exists.")
        total, entries = self._reader.page_folder_entries(
            root_id,
            relative_folder,
            include_subfolders=include_subfolders,
            offset=offset,
            limit=limit,
        )
        items = [self._image_view(entry) for entry in entries]
        return {
            "library_id": self.library_id,
            "folder_key": folder_key,
            "root_id": root_id,
            "relative_folder": relative_folder,
            "include_subfolders": include_subfolders,
            "items": items,
            "offset": offset,
            "limit": limit,
            "total": total,
            "has_more": offset + len(items) < total,
        }

    def resolve_image_path(self, doc_id: str) -> Path:
        """Resolve locally for image registration without exposing it in JSON."""

        return self._reader.resolve_document_path(doc_id)

    def decode_folder_key(self, folder_key: str) -> tuple[str, str]:
        return self._decode_folder_key(folder_key)

    def close(self) -> None:
        self._reader.close()

    def __enter__(self) -> LibraryBrowser:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _folder_key(self, root_id: str, relative_folder: str) -> str:
        payload = json.dumps(
            {
                "library_id": self.library_id,
                "collection_uuid": self.collection_uuid,
                "root_id": root_id,
                "relative_folder": relative_folder,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return f"{_FOLDER_KEY_PREFIX}{encoded}"

    def _decode_folder_key(self, folder_key: str) -> tuple[str, str]:
        if not isinstance(folder_key, str) or not folder_key.startswith(
            _FOLDER_KEY_PREFIX
        ):
            raise ValueError("folder_key is invalid or unsupported.")
        encoded = folder_key[len(_FOLDER_KEY_PREFIX) :]
        if not encoded or len(encoded) > _MAX_FOLDER_KEY_BYTES:
            raise ValueError("folder_key has an invalid length.")
        try:
            padding = "=" * (-len(encoded) % 4)
            decoded = base64.b64decode(
                encoded + padding,
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(decoded.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("folder_key is invalid.") from exc
        if not isinstance(payload, dict):
            raise ValueError("folder_key is invalid.")
        if payload.get("library_id") != self.library_id:
            raise ValueError("folder_key belongs to a different library.")
        if payload.get("collection_uuid") != self.collection_uuid:
            raise ValueError("folder_key belongs to a replaced Collection.")
        root_id = payload.get("root_id")
        relative_folder = payload.get("relative_folder")
        if not isinstance(root_id, str) or not root_id.strip():
            raise ValueError("folder_key has no valid root_id.")
        if not isinstance(relative_folder, str):
            raise ValueError("folder_key has no valid relative folder.")
        normalized = relative_folder.replace("\\", "/").strip("/")
        parts = PurePosixPath(normalized).parts if normalized else ()
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("folder_key contains an unsafe relative folder.")
        return root_id.strip(), "/".join(parts)

    @staticmethod
    def _image_view(entry: dict[str, Any]) -> dict[str, Any]:
        source_groups: tuple[tuple[str, Iterable[str]], ...] = (
            ("manual", entry.get("tags", ())),
            ("folder", entry.get("folder_tags", ())),
            ("model", entry.get("accepted_auto_tags", ())),
            ("inherited", entry.get("inherited_tags", ())),
        )
        by_tag: dict[str, list[str]] = {}
        for source, tags in source_groups:
            for tag in tags:
                sources = by_tag.setdefault(str(tag), [])
                if source not in sources:
                    sources.append(source)
        return {
            "doc_id": str(entry["doc_id"]),
            "root_id": str(entry["root_id"]),
            "relative_path": str(entry["relative_path"]),
            "file_name": str(entry["file_name"]),
            "extension": str(entry["extension"]),
            "mime_type": str(entry["mime_type"]),
            "width": int(entry["width"]),
            "height": int(entry["height"]),
            "size_bytes": int(entry["size_bytes"]),
            "manual_tags": list(entry.get("tags", ())),
            "folder_tags": list(entry.get("folder_tags", ())),
            "model_tags": list(entry.get("accepted_auto_tags", ())),
            "inherited_tags": list(entry.get("inherited_tags", ())),
            "effective_tags": list(entry.get("effective_tags", ())),
            "tag_sources": [
                {"tag": tag, "sources": sources} for tag, sources in by_tag.items()
            ],
            "annotation_status": str(entry.get("annotation_status") or ""),
            "annotation_policy": dict(entry.get("annotation_policy") or {}),
        }
