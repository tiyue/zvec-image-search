from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigurationError, ServiceConfig

LIBRARY_CONFIG_SCHEMA_VERSION = 3
LEGACY_LIBRARY_CONFIG_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class LibraryDefinition:
    library_id: str
    name: str
    image_root: Path | None
    workspace: Path
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.library_id,
            "name": self.name,
            "image_root": str(self.image_root) if self.image_root else None,
            "workspace_directory": str(self.workspace),
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class LibraryCatalog:
    default_library_id: str
    libraries: tuple[LibraryDefinition, ...]
    federated_results_directory: Path

    @property
    def by_id(self) -> dict[str, LibraryDefinition]:
        return {library.library_id: library for library in self.libraries}

    @property
    def enabled(self) -> tuple[LibraryDefinition, ...]:
        return tuple(library for library in self.libraries if library.enabled)

    @property
    def default(self) -> LibraryDefinition:
        return self.by_id[self.default_library_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LIBRARY_CONFIG_SCHEMA_VERSION,
            "default_library_id": self.default_library_id,
            "results_directory": str(self.federated_results_directory),
            "libraries": [library.to_dict() for library in self.libraries],
        }


def load_library_catalog(
    path: str | Path | None,
    fallback_config: ServiceConfig,
) -> LibraryCatalog:
    if path is None:
        return _fallback_catalog(fallback_config)
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise ConfigurationError(f"Libraries config is missing: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"Libraries config is not valid UTF-8 JSON: {manifest_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("Libraries config must contain a JSON object.")
    schema_version = payload.get("schema_version")
    if schema_version not in {
        LEGACY_LIBRARY_CONFIG_SCHEMA_VERSION,
        LIBRARY_CONFIG_SCHEMA_VERSION,
    }:
        raise ConfigurationError(
            "Libraries config schema_version must be "
            f"{LEGACY_LIBRARY_CONFIG_SCHEMA_VERSION} or "
            f"{LIBRARY_CONFIG_SCHEMA_VERSION}."
        )
    default_library_id = _required_text(payload, "default_library_id")
    raw_libraries = payload.get("libraries")
    if not isinstance(raw_libraries, list) or not raw_libraries:
        raise ConfigurationError(
            "Libraries config requires a non-empty libraries array."
        )

    libraries: list[LibraryDefinition] = []
    seen_ids: set[str] = set()
    for index, raw_library in enumerate(raw_libraries):
        if not isinstance(raw_library, dict):
            raise ConfigurationError(f"libraries[{index}] must be an object.")
        library_id = _required_text(raw_library, "id", prefix=f"libraries[{index}].")
        if library_id in seen_ids:
            raise ConfigurationError(f"Duplicate library id: {library_id}")
        seen_ids.add(library_id)
        name = _required_text(raw_library, "name", prefix=f"libraries[{index}].")
        enabled = raw_library.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigurationError(f"libraries[{index}].enabled must be a boolean.")
        image_root = _optional_absolute_path(
            raw_library.get("image_root"), f"libraries[{index}].image_root"
        )
        workspace_field = (
            "workspace_directory"
            if schema_version == LIBRARY_CONFIG_SCHEMA_VERSION
            else "workspace"
        )
        workspace = _required_absolute_path(
            raw_library.get(workspace_field),
            f"libraries[{index}].{workspace_field}",
        )
        libraries.append(
            LibraryDefinition(
                library_id=library_id,
                name=name,
                image_root=image_root,
                workspace=workspace,
                enabled=enabled,
            )
        )

    by_id = {library.library_id: library for library in libraries}
    default = by_id.get(default_library_id)
    if default is None:
        raise ConfigurationError("default_library_id does not identify a library.")
    if not default.enabled:
        raise ConfigurationError("The default library must be enabled.")
    if not any(library.enabled for library in libraries):
        raise ConfigurationError("Libraries config must enable at least one library.")
    federated_results = (
        _optional_absolute_path(payload.get("results_directory"), "results_directory")
        or fallback_config.results_path
    )
    return LibraryCatalog(
        default_library_id=default_library_id,
        libraries=tuple(libraries),
        federated_results_directory=federated_results,
    )


def _fallback_catalog(config: ServiceConfig) -> LibraryCatalog:
    library = LibraryDefinition(
        library_id="default",
        name="Default Library",
        image_root=None,
        workspace=config.workspace,
    )
    return LibraryCatalog(
        default_library_id=library.library_id,
        libraries=(library,),
        federated_results_directory=config.results_path,
    )


def _required_text(payload: dict[str, Any], name: str, *, prefix: str = "") -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{prefix}{name} must be a non-empty string.")
    return value.strip()


def _required_absolute_path(value: Any, label: str) -> Path:
    path = _optional_absolute_path(value, label)
    if path is None:
        raise ConfigurationError(f"{label} must be an absolute path.")
    return path


def _optional_absolute_path(value: Any, label: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be an absolute path or null.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{label} must be an absolute path.")
    return path.resolve()
