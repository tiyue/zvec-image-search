from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .config import ConfigurationError


def normalize_path(path: Path) -> str:
    return os.path.normcase(str(path.expanduser().resolve()))


def normalize_relative_path(value: str | Path) -> str:
    normalized = str(value).replace("\\", "/").strip("/")
    if not normalized or normalized == ".":
        raise ConfigurationError("Relative image path cannot be empty.")
    return normalized


def logical_document_id(root_id: str, relative_path: str | Path) -> str:
    logical_path = normalize_relative_path(relative_path).casefold()
    return hashlib.sha256(f"{root_id}\0{logical_path}".encode()).hexdigest()


def resolve_under_root(root_path: str | Path, relative_path: str | Path) -> Path:
    root = Path(root_path).expanduser().resolve()
    candidate = (root / normalize_relative_path(relative_path)).resolve()
    try:
        if os.path.commonpath([normalize_path(root), normalize_path(candidate)]) != (
            normalize_path(root)
        ):
            raise ConfigurationError("Image path escapes its registered root.")
    except ValueError as exc:
        raise ConfigurationError(
            "Image path is on a different filesystem root."
        ) from exc
    return candidate
