from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class IndexState:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.entries = {}
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.entries = dict(data.get("entries") or {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "entries": self.entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def get(self, doc_id: str) -> dict[str, Any] | None:
        return self.entries.get(doc_id)

    def set(self, doc_id: str, value: dict[str, Any]) -> None:
        self.entries[doc_id] = value

    def remove(self, doc_id: str) -> None:
        self.entries.pop(doc_id, None)

    def ids_for_root(self, root_path: str) -> set[str]:
        return {
            doc_id
            for doc_id, entry in self.entries.items()
            if entry.get("root_path") == root_path
        }

    def find_doc_id_by_sha(self, sha256: str) -> str | None:
        for doc_id, entry in self.entries.items():
            if entry.get("sha256") == sha256:
                return doc_id
        return None
