from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_SCHEMA_VERSION = 3
LEGACY_DOCKER_ROOT = "/data/roots/main"
DOCKER_VOLUME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")
DOCKER_REPOSITORY_COMPONENT_PATTERN = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*$"
)
DOCKER_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
DOCKER_DIGEST_PATTERN = re.compile(r"^sha256:[A-Fa-f0-9]{64}$")
DOCKER_EXPORT_RECEIPT = ".zvec-docker-volume-export.json"
DOCKER_EXPORT_RECEIPT_SCHEMA_VERSION = 1


class LauncherError(RuntimeError):
    """A user-actionable native launcher error."""


class LegacyDockerVolumeRequired(LauncherError):
    """Raised when a legacy named volume needs an explicit one-time export."""


@dataclass(frozen=True)
class NativeLibrary:
    library_id: str
    name: str
    image_root: Path
    workspace_directory: Path
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.library_id,
            "name": self.name,
            "image_root": str(self.image_root),
            "workspace_directory": str(self.workspace_directory),
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class NativeConfig:
    default_library_id: str
    results_directory: Path
    libraries: tuple[NativeLibrary, ...]
    python_executable: str | None = None

    @property
    def by_id(self) -> dict[str, NativeLibrary]:
        return {library.library_id: library for library in self.libraries}

    def select(self, selector: str | None = None) -> NativeLibrary:
        requested = (selector or os.getenv("ZVEC_LIBRARY_ID") or "").strip()
        if not requested:
            requested = self.default_library_id
        folded = requested.casefold()
        matches = [
            library
            for library in self.libraries
            if library.library_id.casefold() == folded
            or library.name.casefold() == folded
        ]
        if len(matches) != 1:
            raise LauncherError(f"Library was not found or is ambiguous: {requested}")
        return matches[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            **(
                {"python_executable": self.python_executable}
                if self.python_executable is not None
                else {}
            ),
            "results_directory": str(self.results_directory),
            "default_library_id": self.default_library_id,
            "libraries": [library.to_dict() for library in self.libraries],
        }


def get_config_home() -> Path:
    configured = os.getenv("ZVEC_CONFIG_HOME")
    legacy = os.getenv("ZVEC_DOCKER_CONFIG_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    if legacy:
        return Path(legacy).expanduser().resolve()
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"], "zvec-image-search").resolve()
    return Path.home().joinpath(".zvec-image-search").resolve()


def get_config_path() -> Path:
    return get_config_home() / "config.json"


def get_environment_path() -> Path:
    return get_config_home() / ".env"


def get_model_config_path() -> Path:
    return get_config_home() / "models.json"


def ensure_model_config() -> Path:
    from image_vector_service.model_catalog import ensure_user_model_configuration

    try:
        return ensure_user_model_configuration(get_model_config_path())
    except (OSError, ValueError) as exc:
        raise LauncherError(
            f"Invalid Alibaba Cloud model configuration: {exc}"
        ) from exc


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise LauncherError(f"{label} must be a non-empty absolute path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise LauncherError(f"{label} must be an absolute path: {value}")
    return path.resolve()


def _required_text(payload: Mapping[str, Any], name: str, label: str = "") -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise LauncherError(f"{label}{name} must be a non-empty string.")
    return value.strip()


def _validate_library_id(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", normalized):
        raise LauncherError(f"Invalid library id: {value}")
    return normalized


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        common = os.path.commonpath((str(left), str(right)))
    except ValueError:
        # Different Windows drives cannot overlap.
        return False
    normalized_common = os.path.normcase(os.path.abspath(common))
    return normalized_common in {
        os.path.normcase(os.path.abspath(str(left))),
        os.path.normcase(os.path.abspath(str(right))),
    }


def validate_native_config(payload: Mapping[str, Any]) -> NativeConfig:
    if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise LauncherError(
            f"Launcher config schema_version must be {CONFIG_SCHEMA_VERSION}."
        )
    results_directory = _absolute_path(
        payload.get("results_directory"), "results_directory"
    )
    raw_python_executable = payload.get("python_executable")
    if raw_python_executable is None:
        python_executable = None
    elif (
        not isinstance(raw_python_executable, str) or not raw_python_executable.strip()
    ):
        raise LauncherError("python_executable must be a non-empty string or null.")
    else:
        # This may intentionally be either an absolute path or a command resolved
        # by the desktop host. Keep it opaque while normalizing surrounding space.
        python_executable = raw_python_executable.strip()
    default_library_id = _validate_library_id(
        _required_text(payload, "default_library_id")
    )
    raw_libraries = payload.get("libraries")
    if not isinstance(raw_libraries, list) or not raw_libraries:
        raise LauncherError("Launcher config requires a non-empty libraries array.")

    libraries: list[NativeLibrary] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    seen_workspaces: set[str] = set()
    for index, value in enumerate(raw_libraries):
        if not isinstance(value, Mapping):
            raise LauncherError(f"libraries[{index}] must be an object.")
        label = f"libraries[{index}]."
        library_id = _validate_library_id(_required_text(value, "id", label))
        name = _required_text(value, "name", label)
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise LauncherError(f"{label}enabled must be a boolean.")
        image_root = _absolute_path(value.get("image_root"), f"{label}image_root")
        workspace = _absolute_path(
            value.get("workspace_directory"), f"{label}workspace_directory"
        )
        name_key = name.casefold()
        workspace_key = os.path.normcase(str(workspace))
        if library_id in seen_ids:
            raise LauncherError(f"Duplicate library id: {library_id}")
        if name_key in seen_names:
            raise LauncherError(f"Duplicate library name: {name}")
        if workspace_key in seen_workspaces:
            raise LauncherError(
                f"Multiple libraries cannot share a workspace: {workspace}"
            )
        seen_ids.add(library_id)
        seen_names.add(name_key)
        seen_workspaces.add(workspace_key)
        libraries.append(
            NativeLibrary(
                library_id=library_id,
                name=name,
                image_root=image_root,
                workspace_directory=workspace,
                enabled=enabled,
            )
        )

    by_id = {library.library_id: library for library in libraries}
    default = by_id.get(default_library_id)
    if default is None:
        raise LauncherError("default_library_id does not identify a library.")
    if not default.enabled:
        raise LauncherError("The default library must be enabled.")
    if not any(library.enabled for library in libraries):
        raise LauncherError("At least one library must be enabled.")
    for library in libraries:
        if _paths_overlap(results_directory, library.image_root):
            raise LauncherError(
                f"Results directory cannot overlap image root '{library.name}'."
            )
        if _paths_overlap(results_directory, library.workspace_directory):
            raise LauncherError(
                f"Results directory cannot overlap workspace '{library.name}'."
            )
        for image_library in libraries:
            if _paths_overlap(library.workspace_directory, image_library.image_root):
                raise LauncherError(
                    f"Workspace '{library.name}' cannot overlap image root "
                    f"'{image_library.name}'."
                )
    for left_index, left in enumerate(libraries):
        for right in libraries[left_index + 1 :]:
            if _paths_overlap(left.workspace_directory, right.workspace_directory):
                raise LauncherError(
                    f"Workspaces cannot overlap: '{left.name}' and '{right.name}'."
                )
    return NativeConfig(
        default_library_id=default_library_id,
        results_directory=results_directory,
        libraries=tuple(libraries),
        python_executable=python_executable,
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LauncherError(f"Could not read launcher config: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LauncherError(f"Launcher config is invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise LauncherError("Launcher config must contain a JSON object.")
    return value


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_config(config: NativeConfig) -> None:
    validated = validate_native_config(config.to_dict())
    _atomic_write_json(get_config_path(), validated.to_dict())


def _legacy_library_id(image_root: str, workspace_source: str) -> str:
    identity = f"{image_root.upper()}\0{workspace_source.upper()}".encode()
    return f"lib-{hashlib.sha256(identity).hexdigest()[:20]}"


def _legacy_libraries(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    version = payload.get("schema_version")
    if version == 1:
        image_root = _required_text(payload, "image_root")
        workspace_source = _required_text(payload, "workspace_source")
        library_id = _legacy_library_id(image_root, workspace_source)
        name = Path(image_root).name or "Default library"
        return (
            [
                {
                    "id": library_id,
                    "name": name,
                    "image_root": image_root,
                    "workspace_type": _required_text(payload, "workspace_type"),
                    "workspace_source": workspace_source,
                    "enabled": True,
                }
            ],
            library_id,
        )
    if version != 2:
        raise LauncherError(f"Unsupported launcher config version: {version}")
    raw_libraries = payload.get("libraries")
    if not isinstance(raw_libraries, list) or not raw_libraries:
        raise LauncherError("Legacy launcher config requires libraries.")
    libraries: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_libraries):
        if not isinstance(raw, Mapping):
            raise LauncherError(f"Legacy libraries[{index}] must be an object.")
        libraries.append(dict(raw))
    default_id = _required_text(payload, "default_library_id")
    return libraries, default_id


def _native_from_legacy(
    payload: Mapping[str, Any],
    volume_directories: Mapping[str, Path] | None = None,
) -> NativeConfig:
    raw_libraries, default_id = _legacy_libraries(payload)
    volume_directories = volume_directories or {}
    libraries: list[dict[str, Any]] = []
    unresolved: list[tuple[str, str]] = []
    for index, raw in enumerate(raw_libraries):
        label = f"legacy libraries[{index}]."
        library_id = _required_text(raw, "id", label)
        workspace_type = _required_text(raw, "workspace_type", label).lower()
        workspace_source = _required_text(raw, "workspace_source", label)
        if workspace_type == "bind":
            workspace = Path(workspace_source).expanduser().resolve()
        elif workspace_type == "volume":
            workspace_candidate = volume_directories.get(
                library_id
            ) or volume_directories.get(workspace_source)
            if workspace_candidate is None:
                unresolved.append((library_id, workspace_source))
                continue
            workspace = Path(workspace_candidate).expanduser().resolve()
        else:
            raise LauncherError(f"{label}workspace_type must be 'bind' or 'volume'.")
        libraries.append(
            {
                "id": library_id,
                "name": _required_text(raw, "name", label),
                "image_root": _required_text(raw, "image_root", label),
                "workspace_directory": str(workspace),
                "enabled": raw.get("enabled", True),
            }
        )
    if unresolved:
        descriptions = ", ".join(
            f"{library_id}={volume}" for library_id, volume in unresolved
        )
        raise LegacyDockerVolumeRequired(
            "Legacy Docker named volumes require an explicit one-time export "
            f"({descriptions}). Run 'zvec migrate-docker-workspace'."
        )
    results = _required_text(payload, "results_directory")
    return validate_native_config(
        {
            "schema_version": CONFIG_SCHEMA_VERSION,
            **(
                {"python_executable": payload["python_executable"]}
                if "python_executable" in payload
                else {}
            ),
            "results_directory": str(Path(results).expanduser().resolve()),
            "default_library_id": default_id,
            "libraries": libraries,
        }
    )


def _backup_legacy_config(path: Path, schema_version: Any) -> Path:
    backup = path.with_name(f"config.v{schema_version}.docker.backup.json")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def _prepare_workspace_migration_backup(
    *,
    config: NativeConfig,
    libraries: Sequence[NativeLibrary],
    config_path: Path,
    destination: str | None,
    full_backup: bool,
    dry_run: bool,
) -> dict[str, Any]:
    from image_vector_service.workspace_backup import (
        WorkspaceBackupError,
        WorkspaceBackupSource,
        create_migration_backup,
        new_backup_destination,
        plan_migration_backup,
    )

    backup_destination = (
        Path(destination).expanduser().resolve()
        if destination
        else new_backup_destination(get_config_home())
    )
    sources = [
        WorkspaceBackupSource(
            library.library_id,
            library.name,
            library.workspace_directory,
        )
        for library in libraries
    ]
    plan = plan_migration_backup(
        config_path=config_path,
        sources=sources,
        destination=backup_destination,
        full_backup=full_backup,
    )
    if dry_run:
        return {"status": "planned", "plan": plan, "api_requests": 0}
    try:
        return create_migration_backup(
            config_path=config_path,
            sources=sources,
            destination=backup_destination,
            full_backup=full_backup,
        )
    except WorkspaceBackupError as exc:
        raise LauncherError(f"Workspace migration backup failed: {exc}") from exc


def load_config(
    *, optional: bool = False, auto_migrate_bind: bool = True
) -> NativeConfig | None:
    path = get_config_path()
    if not path.is_file():
        if optional:
            return None
        raise LauncherError(
            "Zvec is not configured. Run 'zvec init <image-folder>' first."
        )
    payload = _read_json(path)
    if payload.get("schema_version") == CONFIG_SCHEMA_VERSION:
        return validate_native_config(payload)
    if not auto_migrate_bind:
        raise LauncherError("Legacy launcher config has not been migrated.")
    config = _native_from_legacy(payload)
    _backup_legacy_config(path, payload.get("schema_version"))
    backup = _prepare_workspace_migration_backup(
        config=config,
        libraries=config.libraries,
        config_path=path,
        destination=None,
        full_backup=False,
        dry_run=False,
    )
    for library in config.libraries:
        repair_and_verify_workspace(
            library,
            config.results_directory,
            dry_run=False,
        )
    save_config(config)
    print(
        "Migrated and verified legacy bind-workspace config as native schema v3. "
        f"Backup: {backup['destination']}",
        file=sys.stderr,
    )
    return config


def _replace_library(config: NativeConfig, updated: NativeLibrary) -> NativeConfig:
    libraries = tuple(
        updated if item.library_id == updated.library_id else item
        for item in config.libraries
    )
    return validate_native_config(
        {
            **config.to_dict(),
            "libraries": [library.to_dict() for library in libraries],
        }
    )


def _new_library_id(image_root: Path, workspace: Path) -> str:
    identity = (
        f"{os.path.normcase(str(image_root))}\0{os.path.normcase(str(workspace))}"
    )
    return f"lib-{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"


def _ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _resolve_existing_directory(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise LauncherError(f"{label} does not exist or is not a directory: {path}")
    return path


def _resolve_existing_file(value: str, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise LauncherError(f"{label} does not exist or is not a file: {path}")
    return path


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise LauncherError(f"Could not read API environment file: {path}") from exc
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if not separator or not key.strip():
            raise LauncherError(f"Invalid .env entry at {path}:{line_number}")
        normalized_key = key.strip()
        if normalized_key not in {"DASHSCOPE_API_KEY", "DASHSCOPE_API_URL"}:
            continue
        decoded = value.strip()
        if len(decoded) >= 2 and decoded[0] == decoded[-1] and decoded[0] in "\"'":
            decoded = decoded[1:-1]
        values[normalized_key] = decoded
    return values


@contextlib.contextmanager
def _runtime_environment(results_directory: Path) -> Iterator[None]:
    updates = _read_dotenv(get_environment_path())
    updates["ZVEC_IMAGE_RESULTS_DIR"] = str(results_directory)
    updates["ZVEC_MODELS_CONFIG"] = str(ensure_model_config())
    previous = {name: os.environ.get(name) for name in updates}
    try:
        for name, value in updates.items():
            if name in {"DASHSCOPE_API_KEY", "DASHSCOPE_API_URL"} and os.getenv(name):
                continue
            os.environ[name] = value
        yield
    finally:
        for name, previous_value in previous.items():
            if previous_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous_value


def _run_core(
    config: NativeConfig,
    library: NativeLibrary,
    arguments: Sequence[str],
) -> int:
    from image_service import main as core_main

    library.workspace_directory.mkdir(parents=True, exist_ok=True)
    config.results_directory.mkdir(parents=True, exist_ok=True)
    with _runtime_environment(config.results_directory):
        return int(
            core_main(["--workspace", str(library.workspace_directory), *arguments])
        )


def _save_api_environment(skip_key: bool) -> None:
    if skip_key:
        return
    path = get_environment_path()
    if path.is_file():
        return
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        if not sys.stdin.isatty():
            print(
                "DashScope API key was not saved; set DASHSCOPE_API_KEY "
                "before indexing.",
                file=sys.stderr,
            )
            return
        api_key = input("DASHSCOPE_API_KEY: ").strip()
    if not api_key or "\n" in api_key or "\r" in api_key:
        raise LauncherError("DASHSCOPE_API_KEY cannot be empty or contain a newline.")
    lines = [f"DASHSCOPE_API_KEY={api_key}"]
    api_url = os.getenv("DASHSCOPE_API_URL", "").strip()
    if api_url:
        lines.append(f"DASHSCOPE_API_URL={api_url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def _init(arguments: Sequence[str], selector: str | None) -> int:
    parser = argparse.ArgumentParser(prog="zvec init", add_help=True)
    parser.add_argument("image_folder")
    parser.add_argument("--workspace")
    parser.add_argument("--results")
    parser.add_argument("--skip-key", action="store_true")
    # Accepted only to provide a precise migration message to existing users.
    parser.add_argument("--workspace-volume")
    parser.add_argument("--image-name", help=argparse.SUPPRESS)
    parser.add_argument("--no-build", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(list(arguments))
    if args.workspace_volume:
        raise LauncherError(
            "--workspace-volume is no longer supported for new configurations. "
            "Use --workspace with a host directory."
        )
    image_root = _resolve_existing_directory(args.image_folder, "Image folder")
    existing = load_config(optional=True)
    existing_library = existing.select(selector) if existing is not None else None
    if args.workspace:
        workspace = _ensure_directory(Path(args.workspace).expanduser().resolve())
    elif existing_library is not None and existing_library.image_root == image_root:
        workspace = existing_library.workspace_directory
    else:
        provisional = hashlib.sha256(
            os.path.normcase(str(image_root)).encode("utf-8")
        ).hexdigest()[:12]
        workspace = _ensure_directory(
            get_config_home() / "libraries" / provisional / "workspace"
        )
    results = (
        _ensure_directory(Path(args.results).expanduser().resolve())
        if args.results
        else (
            existing.results_directory
            if existing is not None
            else _ensure_directory(get_config_home() / "results")
        )
    )
    if existing_library is None:
        library_id = _new_library_id(image_root, workspace)
        name = image_root.name or "Default library"
        library = NativeLibrary(library_id, name, image_root, workspace, True)
        config = NativeConfig(library_id, results, (library,))
    else:
        assert existing is not None
        library = NativeLibrary(
            existing_library.library_id,
            existing_library.name,
            image_root,
            workspace,
            existing_library.enabled,
        )
        config = _replace_library(existing, library)
        if config.results_directory != results:
            config = NativeConfig(
                config.default_library_id,
                results,
                config.libraries,
                config.python_executable,
            )
    save_config(config)
    ensure_model_config()
    _save_api_environment(args.skip_key)
    print(f"Configured library:    {library.name} ({library.library_id})")
    print(f"Configured image root: {library.image_root}")
    print(f"Configured workspace:  {library.workspace_directory}")
    print(f"Configured results:    {config.results_directory}")
    print("Runtime:               native Python (Docker is not required)")
    print("Run 'zvec index' to build the image index.")
    return 0


def _library_add(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="zvec library-add")
    parser.add_argument("name")
    parser.add_argument("image_folder")
    parser.add_argument("--workspace")
    parser.add_argument("--workspace-volume")
    parser.add_argument("--disabled", action="store_true")
    args = parser.parse_args(list(arguments))
    if args.workspace_volume:
        raise LauncherError(
            "New libraries require --workspace <host-directory>; Docker named "
            "volumes are supported only by migrate-docker-workspace."
        )
    config = load_config()
    assert config is not None
    name = args.name.strip()
    if not name:
        raise LauncherError("Library name cannot be empty.")
    image_root = _resolve_existing_directory(args.image_folder, "Image folder")
    provisional_id = f"lib-{uuid.uuid4().hex}"
    workspace = _ensure_directory(
        Path(args.workspace).expanduser().resolve()
        if args.workspace
        else get_config_home() / "libraries" / provisional_id / "workspace"
    )
    library_id = _new_library_id(image_root, workspace)
    library = NativeLibrary(library_id, name, image_root, workspace, not args.disabled)
    updated = validate_native_config(
        {
            **config.to_dict(),
            "libraries": [
                *[item.to_dict() for item in config.libraries],
                library.to_dict(),
            ],
        }
    )
    save_config(updated)
    print(f"Added library: {name} ({library_id})")
    return 0


def _library_mutation(command: str, arguments: Sequence[str]) -> int:
    config = load_config()
    assert config is not None
    if command == "library-rename":
        if len(arguments) != 2 or not arguments[1].strip():
            raise LauncherError("Usage: zvec library-rename <id-or-name> <new-name>")
        selected = config.select(arguments[0])
        updated_library = NativeLibrary(
            selected.library_id,
            arguments[1].strip(),
            selected.image_root,
            selected.workspace_directory,
            selected.enabled,
        )
        save_config(_replace_library(config, updated_library))
        print(f"Renamed library {selected.library_id} to {updated_library.name}")
        return 0
    if len(arguments) != 1:
        raise LauncherError(f"Usage: zvec {command} <id-or-name>")
    selected = config.select(arguments[0])
    if command in {"library-enable", "library-disable"}:
        enabled = command == "library-enable"
        if not enabled and selected.library_id == config.default_library_id:
            raise LauncherError(
                "Set another default library before disabling this one."
            )
        updated_library = NativeLibrary(
            selected.library_id,
            selected.name,
            selected.image_root,
            selected.workspace_directory,
            enabled,
        )
        save_config(_replace_library(config, updated_library))
        print(f"Library {selected.name} enabled={str(enabled).lower()}")
        return 0
    if command == "library-default":
        if not selected.enabled:
            raise LauncherError("The default library must be enabled.")
        save_config(
            NativeConfig(
                selected.library_id,
                config.results_directory,
                config.libraries,
                config.python_executable,
            )
        )
        print(f"Default library: {selected.name} ({selected.library_id})")
        return 0
    if command == "library-remove":
        remaining = tuple(
            item for item in config.libraries if item.library_id != selected.library_id
        )
        if not remaining:
            raise LauncherError("The final library cannot be removed.")
        default_id = config.default_library_id
        if selected.library_id == default_id:
            replacement = next((item for item in remaining if item.enabled), None)
            if replacement is None:
                raise LauncherError(
                    "Enable another library before removing the default."
                )
            default_id = replacement.library_id
        save_config(
            NativeConfig(
                default_id,
                config.results_directory,
                remaining,
                config.python_executable,
            )
        )
        print(
            f"Removed library configuration: {selected.name}. "
            "Workspace data was preserved."
        )
        return 0
    raise LauncherError(f"Unknown library operation: {command}")


def _is_writable_directory(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".zvec-write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _doctor() -> int:
    failures = 0

    def report(status: str, message: str) -> None:
        print(f"[{status}] {message}")

    report("OK", f"Native Python {platform.python_version()}")
    for package in ("zvec", "Pillow"):
        try:
            version = importlib.metadata.version(package)
            report("OK", f"{package} {version}")
        except importlib.metadata.PackageNotFoundError:
            report("FAIL", f"Python package is missing: {package}")
            failures += 1
    try:
        config = load_config()
        assert config is not None
        report("OK", f"Native launcher config: {get_config_path()}")
    except LauncherError as exc:
        report("FAIL", str(exc))
        return 1
    if _is_writable_directory(config.results_directory):
        report("OK", f"Results directory is writable: {config.results_directory}")
    else:
        report("FAIL", f"Results directory is not writable: {config.results_directory}")
        failures += 1
    for library in config.libraries:
        if library.image_root.is_dir():
            report("OK", f"Image root [{library.name}]: {library.image_root}")
        else:
            report("FAIL", f"Image root is unavailable [{library.name}]")
            failures += 1
        if _is_writable_directory(library.workspace_directory):
            report("OK", f"Workspace is writable [{library.name}]")
        else:
            report("FAIL", f"Workspace is not writable [{library.name}]")
            failures += 1
    credentials = _read_dotenv(get_environment_path())
    if os.getenv("DASHSCOPE_API_KEY") or credentials.get("DASHSCOPE_API_KEY"):
        report("OK", "DashScope API key is configured")
    else:
        report("WARN", "DashScope API key is not configured")
    report("OK", "Docker Desktop, Docker Engine and Compose are not required")
    return 1 if failures else 0


def _open_results(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    if os.getenv("ZVEC_NO_OPEN") == "1":
        print(path)
        return 0
    try:
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except (OSError, subprocess.SubprocessError) as exc:
        raise LauncherError(f"Could not open results directory: {path}") from exc
    print(path)
    return 0


def _run_checked(
    command: Sequence[str], description: str
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise LauncherError(
            f"{description} requires the Docker CLI for one-time export."
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise LauncherError(f"{description} failed: {detail or 'unknown error'}")
    return result


def _validate_registry_component(value: str) -> None:
    host = value
    port_text: str | None = None
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", value)
        if match is None:
            raise LauncherError(f"Invalid Docker registry component: {value}")
        try:
            address = ipaddress.ip_address(match.group(1))
        except ValueError as exc:
            raise LauncherError(f"Invalid Docker registry address: {value}") from exc
        if not isinstance(address, ipaddress.IPv6Address):
            raise LauncherError(f"Bracketed Docker registry must be IPv6: {value}")
        port_text = match.group(2)
    else:
        if value.count(":") > 1:
            raise LauncherError(f"Invalid Docker registry component: {value}")
        if ":" in value:
            host, port_text = value.rsplit(":", 1)
        if len(host) > 253:
            raise LauncherError(f"Docker registry hostname is too long: {host}")
        labels = host.split(".")
        if any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
            for label in labels
        ):
            raise LauncherError(f"Invalid Docker registry hostname: {host}")
    if port_text is not None:
        if not port_text or not port_text.isascii() or not port_text.isdecimal():
            raise LauncherError(f"Invalid Docker registry port: {value}")
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise LauncherError(f"Docker registry port is out of range: {value}")


def _validate_docker_image_reference(value: str) -> str:
    """Validate an OCI/Docker image reference before it enters Docker's option list."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or not value.isascii()
    ):
        raise LauncherError("Legacy image_name is not a valid Docker image reference.")
    reference = value
    if reference.count("@") > 1:
        raise LauncherError(f"Invalid Docker image reference: {value}")
    if "@" in reference:
        reference, digest = reference.rsplit("@", 1)
        if DOCKER_DIGEST_PATTERN.fullmatch(digest) is None:
            raise LauncherError(f"Invalid Docker image digest: {value}")

    last_slash = reference.rfind("/")
    last_colon = reference.rfind(":")
    if last_colon > last_slash:
        repository, tag = reference[:last_colon], reference[last_colon + 1 :]
        if DOCKER_TAG_PATTERN.fullmatch(tag) is None:
            raise LauncherError(f"Invalid Docker image tag: {value}")
    else:
        repository = reference
    components = repository.split("/")
    if not components or any(not component for component in components):
        raise LauncherError(f"Invalid Docker image repository: {value}")

    first = components[0]
    if len(components) > 1 and (
        first == "localhost" or "." in first or ":" in first or first.startswith("[")
    ):
        _validate_registry_component(first)
        components = components[1:]
    if not components or any(
        DOCKER_REPOSITORY_COMPONENT_PATTERN.fullmatch(component) is None
        for component in components
    ):
        raise LauncherError(f"Invalid Docker image repository: {value}")
    return value


def _exported_workspace_error(path: Path) -> str | None:
    collection = path / "image_collection"
    metadata = path / "image_collection.meta.json"
    state = path / "image_collection.state.sqlite3"
    if not collection.is_dir():
        return "image_collection directory is missing"
    if not metadata.is_file():
        return "image_collection.meta.json is missing"
    if not state.is_file():
        return "Collection state database is missing"
    return None


def _read_export_receipt(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LauncherError(f"Docker export receipt is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise LauncherError(f"Docker export receipt must be a JSON object: {path}")
    return payload


def _validate_reusable_export(destination: Path, volume: str) -> None:
    receipt_path = destination / DOCKER_EXPORT_RECEIPT
    if not receipt_path.is_file():
        raise LauncherError(
            f"Export destination is not empty and has no verified export receipt: "
            f"{destination}. Choose an empty directory."
        )
    receipt = _read_export_receipt(receipt_path)
    if (
        receipt.get("schema_version") != DOCKER_EXPORT_RECEIPT_SCHEMA_VERSION
        or receipt.get("status") != "complete"
        or receipt.get("volume") != volume
    ):
        raise LauncherError(
            f"Export destination receipt does not match Docker volume '{volume}': "
            f"{destination}"
        )
    error = _exported_workspace_error(destination)
    if error is not None:
        raise LauncherError(f"Existing Docker volume export is incomplete: {error}.")


def export_docker_volume(volume: str, image: str, destination: Path) -> dict[str, Any]:
    if not DOCKER_VOLUME_PATTERN.fullmatch(volume):
        raise LauncherError(f"Invalid Docker volume name: {volume}")
    image = _validate_docker_image_reference(image)
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        _validate_reusable_export(destination, volume)
        return {
            "status": "reused_existing_export",
            "volume": volume,
            "destination": str(destination),
        }
    container_name = f"zvec-workspace-export-{uuid.uuid4().hex[:16]}"
    staging = Path(
        tempfile.mkdtemp(
            prefix=".zvec-docker-volume-export-",
            dir=destination.parent,
        )
    ).resolve()
    created = False
    try:
        _run_checked(
            [
                "docker",
                "create",
                "--name",
                container_name,
                "--mount",
                f"type=volume,source={volume},target=/source,readonly",
                "--entrypoint",
                "/bin/sh",
                "--",
                image,
                "-c",
                "true",
            ],
            f"Creating export container for volume {volume}",
        )
        created = True
        _run_checked(
            ["docker", "cp", f"{container_name}:/source/.", str(staging)],
            f"Exporting Docker volume {volume}",
        )
        error = _exported_workspace_error(staging)
        if error is not None:
            raise LauncherError(
                f"Docker volume {volume} did not contain a complete Zvec workspace: "
                f"{error}."
            )
        _atomic_write_json(
            staging / DOCKER_EXPORT_RECEIPT,
            {
                "schema_version": DOCKER_EXPORT_RECEIPT_SCHEMA_VERSION,
                "status": "complete",
                "volume": volume,
                "image": image,
            },
        )
        # The destination was checked as empty above. Replacing it only after the
        # staged export validates prevents interrupted docker cp runs from being
        # mistaken for completed migrations on retry.
        destination.rmdir()
        staging.replace(destination)
    finally:
        if created:
            # The source volume remains read-only and untouched. A missing CLI
            # during best-effort cleanup must not hide the export/validation result;
            # the uniquely named stopped container can be removed later.
            with contextlib.suppress(OSError):
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "status": "exported",
        "volume": volume,
        "destination": str(destination),
    }


def _workspace_counts(workspace: Path) -> dict[str, int | None]:
    state_path = workspace / "image_collection.state.sqlite3"
    tracked: int | None = None
    if state_path.is_file():
        import sqlite3

        connection = sqlite3.connect(state_path)
        try:
            row = connection.execute("SELECT COUNT(*) FROM entries").fetchone()
            tracked = int(row[0]) if row else 0
        finally:
            connection.close()
    collection_count: int | None = None
    collection_path = workspace / "image_collection"
    if collection_path.exists():
        import zvec

        collection = zvec.open(str(collection_path))
        collection_count = int(collection.stats.doc_count)
        del collection
    return {"collection_documents": collection_count, "sqlite_entries": tracked}


def repair_and_verify_workspace(
    library: NativeLibrary,
    results_directory: Path,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    from image_vector_service.config import ServiceConfig
    from image_vector_service.path_migration import migrate_schema

    workspace = library.workspace_directory
    meta_path = workspace / "image_collection.meta.json"
    collection_path = workspace / "image_collection"
    if not meta_path.exists() and not collection_path.exists():
        return {
            "library_id": library.library_id,
            "workspace": str(workspace),
            "status": "no_index",
            "api_requests": 0,
        }
    if not meta_path.is_file() or not collection_path.exists():
        raise LauncherError(
            f"Incomplete Zvec workspace for {library.name}: {workspace}"
        )
    service_config = ServiceConfig(
        workspace=workspace,
        results_directory=results_directory,
    )
    before = _workspace_counts(workspace)
    migration = migrate_schema(service_config, dry_run=dry_run)
    if dry_run:
        return {
            "library_id": library.library_id,
            "workspace": str(workspace),
            "status": "dry_run",
            "before": before,
            "schema_migration": migration,
            "api_requests": 0,
        }

    from image_vector_service import ImageVectorService

    service = ImageVectorService(config=service_config)
    try:
        roots = service.list_roots()
        rebind_reports: list[dict[str, Any]] = []
        candidates = [
            root
            for root in roots
            if str(root.get("current_path", "")).replace("\\", "/")
            == LEGACY_DOCKER_ROOT
        ]
        if not candidates and len(roots) == 1:
            candidates = roots
        for root in candidates:
            current = Path(str(root["current_path"])).expanduser()
            try:
                already_bound = current.resolve() == library.image_root.resolve()
            except OSError:
                already_bound = False
            if not already_bound:
                rebind_reports.append(
                    service.rebind_root(str(root["root_id"]), str(library.image_root))
                )
        stats = service.stats()
        collection_count = int(stats["collection_stats"]["doc_count"])
        tracked_count = int(stats["tracked_files"])
        if collection_count != tracked_count:
            raise LauncherError(
                f"Collection/SQLite count mismatch for {library.name}: "
                f"{collection_count} != {tracked_count}"
            )
        search_probe = "not_applicable_empty_collection"
        if tracked_count:
            entry = service.state.list_entries()[0]
            vector = service.repository.fetch_vector(str(entry["doc_id"]))
            if vector is None:
                raise LauncherError(
                    f"Stored vector is missing for {library.name}: {entry['doc_id']}"
                )
            hits = service.repository.query(vector, top_k=1)
            if not hits:
                raise LauncherError(
                    f"Stored-vector search probe returned no result for {library.name}."
                )
            search_probe = "passed"
        return {
            "library_id": library.library_id,
            "workspace": str(workspace),
            "status": "verified",
            "before": before,
            "schema_migration": migration,
            "rebind": rebind_reports,
            "collection_documents": collection_count,
            "sqlite_entries": tracked_count,
            "search_probe": search_probe,
            "api_requests": 0,
        }
    finally:
        service.close()


def _migrate_docker_workspace(arguments: Sequence[str], selector: str | None) -> int:
    parser = argparse.ArgumentParser(prog="zvec migrate-docker-workspace")
    parser.add_argument("--destination")
    parser.add_argument("--backup-directory")
    parser.add_argument("--full-backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    path = get_config_path()
    if not path.is_file():
        raise LauncherError("No launcher config exists to migrate.")
    payload = _read_json(path)
    if payload.get("schema_version") == CONFIG_SCHEMA_VERSION:
        config = validate_native_config(payload)
        selected = config.select(selector)
        workspace_backup = _prepare_workspace_migration_backup(
            config=config,
            libraries=[selected],
            config_path=path,
            destination=args.backup_directory,
            full_backup=args.full_backup,
            dry_run=args.dry_run,
        )
        if args.dry_run and workspace_backup["plan"]["status"] == "blocked":
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "backup": workspace_backup,
                        "api_requests": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        report = repair_and_verify_workspace(
            selected, config.results_directory, dry_run=args.dry_run
        )
        print(
            json.dumps(
                {**report, "backup": workspace_backup},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    raw_libraries, _default_id = _legacy_libraries(payload)
    config_backup: str | None = None
    if not args.dry_run:
        # Schema migration and root rebinding below mutate the workspace. Preserve
        # the exact legacy launcher configuration before the first export or repair
        # so a failed multi-library migration always has an explicit recovery point.
        config_backup = str(_backup_legacy_config(path, payload.get("schema_version")))
    selected_raw: dict[str, Any] | None = None
    if selector:
        folded = selector.casefold()
        matches = [
            library
            for library in raw_libraries
            if str(library.get("id", "")).casefold() == folded
            or str(library.get("name", "")).casefold() == folded
        ]
        if len(matches) != 1:
            raise LauncherError(f"Library was not found or is ambiguous: {selector}")
        selected_raw = matches[0]
    elif len(raw_libraries) == 1:
        selected_raw = raw_libraries[0]
    elif args.destination:
        raise LauncherError("--destination with multiple libraries requires --library.")

    volume_directories: dict[str, Path] = {}
    exports: list[dict[str, Any]] = []
    export_plans: list[tuple[str, Path]] = []
    raw_image_name = payload.get("image_name")
    if raw_image_name is None:
        image_name = "zvec-image-search:local"
    elif not isinstance(raw_image_name, str):
        raise LauncherError(
            "Legacy image_name must be a Docker image reference string."
        )
    else:
        image_name = raw_image_name
    for raw in raw_libraries:
        workspace_type = str(raw.get("workspace_type", "")).lower()
        if workspace_type != "volume":
            continue
        legacy_library_id = _required_text(raw, "id")
        library_id = _validate_library_id(legacy_library_id)
        volume = _required_text(raw, "workspace_source")
        destination = (
            Path(args.destination).expanduser().resolve()
            if args.destination and raw is selected_raw
            else get_config_home() / "libraries" / library_id / "workspace"
        )
        volume_directories[legacy_library_id] = destination
        export_plans.append((volume, destination))

    # Validate every resulting host path and cross-library overlap before Docker
    # writes a byte. This also prevents a crafted legacy library id from escaping
    # the user-scoped default export directory.
    config = _native_from_legacy(payload, volume_directories)
    if export_plans:
        image_name = _validate_docker_image_reference(image_name)
    for volume, destination in export_plans:
        if args.dry_run:
            exports.append(
                {
                    "status": "planned",
                    "volume": volume,
                    "destination": str(destination),
                }
            )
        else:
            exports.append(export_docker_volume(volume, image_name, destination))

    workspace_backup = _prepare_workspace_migration_backup(
        config=config,
        libraries=config.libraries,
        config_path=path,
        destination=args.backup_directory,
        full_backup=args.full_backup,
        dry_run=args.dry_run,
    )
    if args.dry_run and workspace_backup["plan"]["status"] == "blocked":
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "config_schema": CONFIG_SCHEMA_VERSION,
                    "backup": workspace_backup,
                    "exports": exports,
                    "verification": [],
                    "api_requests": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    verification: list[dict[str, Any]] = []
    for library in config.libraries:
        verification.append(
            repair_and_verify_workspace(
                library, config.results_directory, dry_run=args.dry_run
            )
        )
    if not args.dry_run:
        save_config(config)
    print(
        json.dumps(
            {
                "status": "dry_run" if args.dry_run else "migrated",
                "config_schema": CONFIG_SCHEMA_VERSION,
                "backup": config_backup,
                "workspace_backup": workspace_backup,
                "exports": exports,
                "verification": verification,
                "api_requests": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _native_verify(arguments: Sequence[str], selector: str | None) -> int:
    parser = argparse.ArgumentParser(prog="zvec verify-native")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    config = load_config()
    assert config is not None
    selected = config.select(selector)
    report = repair_and_verify_workspace(
        selected, config.results_directory, dry_run=args.dry_run
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _workspace_backup_command(arguments: Sequence[str], selector: str | None) -> int:
    parser = argparse.ArgumentParser(prog="zvec workspace-backup")
    parser.add_argument("--destination")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(arguments))
    path = get_config_path()
    if not path.is_file():
        raise LauncherError("No launcher config exists to back up.")
    payload = _read_json(path)
    if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise LauncherError(
            "Legacy config requires 'zvec migrate-docker-workspace --dry-run' "
            "before a native workspace backup can be created."
        )
    config = validate_native_config(payload)
    libraries: Sequence[NativeLibrary] = (
        [config.select(selector)] if selector else config.libraries
    )
    report = _prepare_workspace_migration_backup(
        config=config,
        libraries=libraries,
        config_path=path,
        destination=args.destination,
        full_backup=args.full,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.dry_run and report["plan"]["status"] == "blocked":
        return 1
    return 0


def _split_library_argument(arguments: Sequence[str]) -> tuple[list[str], str | None]:
    remaining: list[str] = []
    selector: str | None = None
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--library":
            index += 1
            if index >= len(arguments):
                raise LauncherError("--library requires a library id or name.")
            if selector is not None or os.getenv("ZVEC_LIBRARY_ID"):
                raise LauncherError(
                    "Select a library once, using --library or ZVEC_LIBRARY_ID."
                )
            selector = arguments[index]
        else:
            remaining.append(value)
        index += 1
    return remaining, selector


USAGE = """\
Zvec native launcher (Docker is not required)

One-time setup:
  zvec init <image-folder> [--workspace <folder>] [--results <folder>]

Daily commands:
  zvec index [tags...] [options] [--library <id-or-name>]
  zvec sync [options] [--library <id-or-name>]
  zvec search <text> [--tk N] [--tags <tag...>] [--show-low-confidence]
  zvec search-image <image-file> [--tk N] [--tags <tag...>]
  zvec search-mix <image-file> <text> [--tk N] [--tags <tag...>]
  zvec stats | roots | results | clean [days] | cache-clear | doctor

Libraries:
  zvec library-list
  zvec library-add <name> <image-folder> [--workspace <folder>] [--disabled]
  zvec library-rename|library-enable|library-disable|library-default|library-remove ...

Maintenance:
  zvec rebind-root <root-id> [new-image-folder]
  zvec migrate-schema [--dry-run]
  zvec migrate-docker-workspace [--destination <folder>]
      [--backup-directory <folder>] [--full-backup] [--dry-run]
  zvec workspace-backup [--destination <folder>] [--full] [--dry-run]
  zvec verify-native [--dry-run]
  zvec raw <original zvec-image-search arguments>
"""


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0].lower() in {"help", "--help", "-h"}:
        print(USAGE)
        return 0
    command = values.pop(0).lower()
    try:
        values, selector = _split_library_argument(values)
        if command == "init":
            return _init(values, selector)
        if command == "doctor":
            if values:
                raise LauncherError("Usage: zvec doctor")
            return _doctor()
        if command in {"build", "ensure-docker"}:
            if command == "build" and any(
                value not in {"--clean", "--no-cache"} for value in values
            ):
                raise LauncherError("Usage: zvec build [--clean]")
            if command == "ensure-docker" and values:
                raise LauncherError("Usage: zvec ensure-docker")
            print(
                "Native Python runtime is ready; Docker build/engine checks "
                "are no longer required."
            )
            return 0
        if command == "migrate-docker-workspace":
            return _migrate_docker_workspace(values, selector)
        if command == "workspace-backup":
            return _workspace_backup_command(values, selector)
        if command == "verify-native":
            return _native_verify(values, selector)
        if command == "library-list":
            if values:
                raise LauncherError("Usage: zvec library-list")
            config = load_config()
            assert config is not None
            print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
            return 0
        if command == "library-add":
            return _library_add(values)
        if command in {
            "library-rename",
            "library-enable",
            "library-disable",
            "library-default",
            "library-remove",
        }:
            return _library_mutation(command, values)

        config = load_config()
        assert config is not None
        library = config.select(selector)
        if not library.enabled and command not in {"stats", "roots", "raw"}:
            raise LauncherError(f"Library is disabled: {library.name}")
        if command == "results":
            if values:
                raise LauncherError("Usage: zvec results")
            return _open_results(config.results_directory)
        if command == "index":
            return _run_core(
                config, library, ["index", str(library.image_root), *values]
            )
        if command == "sync":
            return _run_core(
                config, library, ["sync", str(library.image_root), *values]
            )
        if command == "search":
            if not values:
                raise LauncherError("Usage: zvec search <text> [options]")
            core_args = (
                ["search", *values]
                if values[0] == "--text"
                else ["search", "--text", values[0], *values[1:]]
            )
            return _run_core(config, library, core_args)
        if command == "search-image":
            if not values:
                raise LauncherError("Usage: zvec search-image <image-file> [options]")
            image = _resolve_existing_file(values[0], "Query image")
            return _run_core(
                config, library, ["search", "--image", str(image), *values[1:]]
            )
        if command == "search-mix":
            if len(values) < 2:
                raise LauncherError(
                    "Usage: zvec search-mix <image-file> <text> [options]"
                )
            image = _resolve_existing_file(values[0], "Query image")
            return _run_core(
                config,
                library,
                ["search", "--image", str(image), "--text", values[1], *values[2:]],
            )
        if command in {"stats", "roots", "cache-clear"}:
            if values:
                raise LauncherError(f"Usage: zvec {command}")
            return _run_core(config, library, [command])
        if command == "clean":
            days = "7"
            remaining = values
            if values and not values[0].startswith("-"):
                try:
                    parsed_days = int(values[0])
                except ValueError as exc:
                    raise LauncherError("clean days must be an integer.") from exc
                if parsed_days < 0:
                    raise LauncherError("clean days cannot be negative.")
                days = str(parsed_days)
                remaining = values[1:]
            return _run_core(
                config, library, ["clean-results", "--days", days, *remaining]
            )
        if command == "rebind-root":
            if not 1 <= len(values) <= 2:
                raise LauncherError(
                    "Usage: zvec rebind-root <root-id> [new-image-folder]"
                )
            new_root = (
                _resolve_existing_directory(values[1], "New image folder")
                if len(values) == 2
                else library.image_root
            )
            code = _run_core(config, library, ["rebind-root", values[0], str(new_root)])
            if code == 0 and new_root != library.image_root:
                save_config(
                    _replace_library(
                        config,
                        NativeLibrary(
                            library.library_id,
                            library.name,
                            new_root,
                            library.workspace_directory,
                            library.enabled,
                        ),
                    )
                )
            return code
        if command in {"migrate-schema", "migrate-path-schema"}:
            return _run_core(config, library, [command, *values])
        if command == "raw":
            if not values:
                raise LauncherError(
                    "Usage: zvec raw <original zvec-image-search arguments>"
                )
            return _run_core(config, library, values)
        raise LauncherError(f"Unknown command '{command}'. Run 'zvec help'.")
    except KeyboardInterrupt:
        print("Interrupted. Completed records remain indexed.", file=sys.stderr)
        return 130
    except (LauncherError, OSError) as exc:
        print(f"zvec: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
