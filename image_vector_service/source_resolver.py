from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import ConfigurationError
from .logical_paths import resolve_under_root
from .models import SearchHit
from .state import IndexState


class SourcePathResolver:
    def __init__(self, state: IndexState):
        self.state = state

    def resolve_fields(self, fields: dict[str, Any]) -> Path:
        root_id = str(fields.get("root_id") or "")
        relative_path = str(fields.get("relative_path") or "")
        root_path = self.state.root_path(root_id)
        if not root_path:
            raise ConfigurationError(f"No path is bound to root_id {root_id!r}.")
        return resolve_under_root(root_path, relative_path)

    def resolve_hit(self, hit: SearchHit) -> Path:
        return self.resolve_fields(hit.fields)
