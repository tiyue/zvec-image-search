"""Native schema-v3 configuration management for the Python desktop client."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zvec_launcher import (
    CONFIG_SCHEMA_VERSION,
    LauncherError,
    NativeConfig,
    NativeLibrary,
    get_config_home,
    validate_native_config,
)

from .model_settings import ModelSettingsError, ModelSettingsService

_MAX_CONFIG_BYTES = 2 * 1024 * 1024


class DesktopConfigurationError(RuntimeError):
    """The native desktop configuration could not be changed safely."""


@dataclass(frozen=True, slots=True)
class ConfigurationSnapshot:
    """Validated configuration plus the exact file used by the desktop."""

    path: Path
    config_home: Path
    configuration: NativeConfig

    @property
    def libraries(self) -> tuple[NativeLibrary, ...]:
        return self.configuration.libraries


class DesktopConfigurationService:
    """Create and edit native config without invoking PowerShell or a CLI."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        config_home: str | Path | None = None,
    ) -> None:
        supplied = Path(config_path).expanduser() if config_path is not None else None
        if config_home is not None:
            home = Path(config_home).expanduser().resolve()
        elif supplied is not None:
            home = supplied.absolute().parent.resolve()
        else:
            home = get_config_home()
        self._config_home = home
        self._path = (
            supplied.absolute()
            if supplied is not None
            else self._config_home / "config.json"
        )
        # Gateway requests are served by a ThreadingHTTPServer.  Serialize the
        # complete read/validate/write transaction so concurrent settings
        # changes cannot silently overwrite one another.
        self._mutation_lock = threading.RLock()

    @property
    def config_home(self) -> Path:
        return self._config_home

    @property
    def path(self) -> Path:
        return self._path

    def load(self, *, optional: bool = False) -> ConfigurationSnapshot | None:
        """Load schema v3; legacy configs are left intact for explicit migration."""

        if not self._path.exists():
            if optional:
                return None
            raise DesktopConfigurationError("尚未配置图库，请先完成首次设置。")
        try:
            _reject_reparse_point(self._path)
            if not self._path.is_file():
                raise DesktopConfigurationError("config.json 必须是普通文件。")
            size = self._path.stat().st_size
            if size <= 0 or size > _MAX_CONFIG_BYTES:
                raise DesktopConfigurationError("config.json 大小无效。")
            payload = json.loads(
                self._path.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_json_object,
            )
            if not isinstance(payload, dict):
                raise DesktopConfigurationError("config.json 必须包含 JSON 对象。")
            if payload.get("schema_version") != CONFIG_SCHEMA_VERSION:
                raise DesktopConfigurationError(
                    "检测到旧版配置，需要先执行工作区迁移后才能启动后端。"
                )
            configuration = validate_native_config(payload)
        except DesktopConfigurationError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, LauncherError) as exc:
            raise DesktopConfigurationError(str(exc)) from exc
        return ConfigurationSnapshot(self._path, self._config_home, configuration)

    def create_initial(
        self,
        image_root: str | Path,
        *,
        name: str | None = None,
        workspace_directory: str | Path | None = None,
        results_directory: str | Path | None = None,
    ) -> ConfigurationSnapshot:
        with self._mutation_lock:
            return self._create_initial(
                image_root,
                name=name,
                workspace_directory=workspace_directory,
                results_directory=results_directory,
            )

    def _create_initial(
        self,
        image_root: str | Path,
        *,
        name: str | None = None,
        workspace_directory: str | Path | None = None,
        results_directory: str | Path | None = None,
    ) -> ConfigurationSnapshot:
        """Create the first local library and the default model JSON."""

        if self._path.exists():
            raise DesktopConfigurationError(
                "config.json 已存在；如需增加图库，请使用“添加图库”。"
            )
        image = _existing_absolute_directory(image_root, "image_root")
        workspace = (
            _ensure_absolute_directory(workspace_directory, "workspace_directory")
            if workspace_directory is not None
            else _ensure_directory(self._default_workspace_for(image))
        )
        results = (
            _ensure_absolute_directory(results_directory, "results_directory")
            if results_directory is not None
            else _ensure_directory(self._config_home / "results")
        )
        library = NativeLibrary(
            library_id=_library_id(image, workspace),
            name=_library_name(name, image),
            image_root=image,
            workspace_directory=workspace,
            enabled=True,
        )
        config = _validate_config(library.library_id, results, (library,))
        self._save(config)
        try:
            ModelSettingsService(self._config_home / "models.json").load(create=True)
        except ModelSettingsError as exc:
            # The launcher config is still valid. Report the adjacent model file
            # failure so first-use UI can offer a retry instead of hiding it.
            raise DesktopConfigurationError(str(exc)) from exc
        return ConfigurationSnapshot(self._path, self._config_home, config)

    def add_library(
        self,
        image_root: str | Path,
        *,
        name: str | None = None,
        workspace_directory: str | Path | None = None,
        enabled: bool = True,
    ) -> ConfigurationSnapshot:
        with self._mutation_lock:
            return self._add_library(
                image_root,
                name=name,
                workspace_directory=workspace_directory,
                enabled=enabled,
            )

    def _add_library(
        self,
        image_root: str | Path,
        *,
        name: str | None = None,
        workspace_directory: str | Path | None = None,
        enabled: bool = True,
    ) -> ConfigurationSnapshot:
        """Add one library while preserving existing Collection directories."""

        if not isinstance(enabled, bool):
            raise DesktopConfigurationError("enabled 必须是布尔值。")
        snapshot = self.load()
        assert snapshot is not None
        image = _existing_absolute_directory(image_root, "image_root")
        workspace = (
            _ensure_absolute_directory(workspace_directory, "workspace_directory")
            if workspace_directory is not None
            else _ensure_directory(self._default_workspace_for(image, unique=True))
        )
        library = NativeLibrary(
            library_id=_library_id(image, workspace),
            name=_library_name(name, image),
            image_root=image,
            workspace_directory=workspace,
            enabled=enabled,
        )
        config = _validate_config(
            snapshot.configuration.default_library_id,
            snapshot.configuration.results_directory,
            (*snapshot.configuration.libraries, library),
            python_executable=snapshot.configuration.python_executable,
        )
        self._save(config)
        return ConfigurationSnapshot(self._path, self._config_home, config)

    def set_default_library(self, library_id: str) -> ConfigurationSnapshot:
        with self._mutation_lock:
            return self._set_default_library(library_id)

    def _set_default_library(self, library_id: str) -> ConfigurationSnapshot:
        snapshot = self.load()
        assert snapshot is not None
        config = _validate_config(
            _required_text(library_id, "library_id"),
            snapshot.configuration.results_directory,
            snapshot.configuration.libraries,
            python_executable=snapshot.configuration.python_executable,
        )
        self._save(config)
        return ConfigurationSnapshot(self._path, self._config_home, config)

    def set_library_enabled(
        self, library_id: str, enabled: bool
    ) -> ConfigurationSnapshot:
        with self._mutation_lock:
            return self._set_library_enabled(library_id, enabled)

    def _set_library_enabled(
        self, library_id: str, enabled: bool
    ) -> ConfigurationSnapshot:
        if not isinstance(enabled, bool):
            raise DesktopConfigurationError("enabled 必须是布尔值。")
        snapshot = self.load()
        assert snapshot is not None
        requested = _required_text(library_id, "library_id")
        found = False
        libraries: list[NativeLibrary] = []
        for library in snapshot.configuration.libraries:
            if library.library_id == requested:
                found = True
                libraries.append(
                    NativeLibrary(
                        library.library_id,
                        library.name,
                        library.image_root,
                        library.workspace_directory,
                        enabled,
                    )
                )
            else:
                libraries.append(library)
        if not found:
            raise DesktopConfigurationError(f"图库不存在：{requested}")
        config = _validate_config(
            snapshot.configuration.default_library_id,
            snapshot.configuration.results_directory,
            tuple(libraries),
            python_executable=snapshot.configuration.python_executable,
        )
        self._save(config)
        return ConfigurationSnapshot(self._path, self._config_home, config)

    def update_library(
        self,
        library_id: str,
        *,
        name: str | None = None,
        image_root: str | Path | None = None,
        workspace_directory: str | Path | None = None,
        enabled: bool | None = None,
        is_default: bool | None = None,
        auto_index_enabled: bool | None = None,
        results_directory: str | Path | None = None,
    ) -> ConfigurationSnapshot:
        """Update one library without changing its stable identity.

        Path changes are validated as one candidate configuration before any
        directory is created or config.json is replaced.  This keeps the old
        configuration usable when a requested path is unsafe or inaccessible.
        """

        with self._mutation_lock:
            return self._update_library(
                library_id,
                name=name,
                image_root=image_root,
                workspace_directory=workspace_directory,
                enabled=enabled,
                is_default=is_default,
                auto_index_enabled=auto_index_enabled,
                results_directory=results_directory,
            )

    def _update_library(
        self,
        library_id: str,
        *,
        name: str | None,
        image_root: str | Path | None,
        workspace_directory: str | Path | None,
        enabled: bool | None,
        is_default: bool | None,
        auto_index_enabled: bool | None,
        results_directory: str | Path | None,
    ) -> ConfigurationSnapshot:
        if enabled is not None and not isinstance(enabled, bool):
            raise DesktopConfigurationError("enabled must be a boolean.")
        if is_default is not None and not isinstance(is_default, bool):
            raise DesktopConfigurationError("is_default must be a boolean.")
        if auto_index_enabled is not None and not isinstance(auto_index_enabled, bool):
            raise DesktopConfigurationError("auto_index_enabled must be a boolean.")

        snapshot = self.load()
        assert snapshot is not None
        requested = _required_text(library_id, "library_id")
        existing = snapshot.configuration.by_id.get(requested)
        if existing is None:
            raise DesktopConfigurationError(f"Library does not exist: {requested}")

        next_image_root = (
            existing.image_root
            if image_root is None
            else _existing_absolute_directory(image_root, "image_root")
        )
        next_workspace = (
            existing.workspace_directory
            if workspace_directory is None
            else _absolute_host_path(workspace_directory, "workspace_directory")
        )
        next_results = (
            snapshot.configuration.results_directory
            if results_directory is None
            else _absolute_host_path(results_directory, "results_directory")
        )
        next_library = NativeLibrary(
            # The id represents the logical library.  Editing host paths must
            # never orphan queued jobs, result manifests, or UI selections.
            library_id=existing.library_id,
            name=existing.name if name is None else _required_text(name, "name"),
            image_root=next_image_root,
            workspace_directory=next_workspace,
            enabled=existing.enabled if enabled is None else enabled,
            auto_index_enabled=(
                existing.auto_index_enabled
                if auto_index_enabled is None
                else auto_index_enabled
            ),
        )
        libraries = tuple(
            next_library if item.library_id == requested else item
            for item in snapshot.configuration.libraries
        )
        default_library_id = snapshot.configuration.default_library_id
        if is_default is True:
            default_library_id = requested
        elif is_default is False and default_library_id == requested:
            raise DesktopConfigurationError(
                "The default library cannot be cleared without selecting another one."
            )

        # Validate the complete graph before creating a requested workspace or
        # results directory.  validate_native_config enforces all cross-library
        # image/workspace/results overlap rules.
        candidate = _validate_config(
            default_library_id,
            next_results,
            libraries,
            python_executable=snapshot.configuration.python_executable,
        )
        if workspace_directory is not None:
            _ensure_directory(candidate.by_id[requested].workspace_directory)
        if results_directory is not None:
            _ensure_directory(candidate.results_directory)

        # Resolve again after creation and revalidate before the atomic replace;
        # this also catches a path redirected by the host filesystem.
        final_libraries = tuple(
            NativeLibrary(
                item.library_id,
                item.name,
                item.image_root,
                (
                    item.workspace_directory.resolve(strict=True)
                    if item.library_id == requested and workspace_directory is not None
                    else item.workspace_directory
                ),
                item.enabled,
            )
            for item in candidate.libraries
        )
        final_results = (
            candidate.results_directory.resolve(strict=True)
            if results_directory is not None
            else candidate.results_directory
        )
        config = _validate_config(
            candidate.default_library_id,
            final_results,
            final_libraries,
            python_executable=candidate.python_executable,
        )
        self._save(config)
        return ConfigurationSnapshot(self._path, self._config_home, config)

    def _default_workspace_for(self, image_root: Path, *, unique: bool = False) -> Path:
        digest = hashlib.sha256(
            os.path.normcase(str(image_root)).encode("utf-8")
        ).hexdigest()[:12]
        suffix = f"-{uuid.uuid4().hex[:8]}" if unique else ""
        return self._config_home / "libraries" / f"{digest}{suffix}" / "workspace"

    def _save(self, config: NativeConfig) -> None:
        try:
            _reject_reparse_point(self._path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _reject_reparse_point(self._path.parent)
            temporary = self._path.with_name(
                f".{self._path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_text(
                    json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(self._path)
            finally:
                temporary.unlink(missing_ok=True)
        except DesktopConfigurationError:
            raise
        except OSError as exc:
            raise DesktopConfigurationError("无法安全保存 config.json。") from exc


def _validate_config(
    default_library_id: str,
    results_directory: Path,
    libraries: tuple[NativeLibrary, ...],
    *,
    python_executable: str | None = None,
) -> NativeConfig:
    try:
        return validate_native_config(
            NativeConfig(
                default_library_id,
                results_directory,
                libraries,
                python_executable,
            ).to_dict()
        )
    except LauncherError as exc:
        raise DesktopConfigurationError(str(exc)) from exc


def _library_id(image_root: Path, workspace: Path) -> str:
    identity = (
        f"{os.path.normcase(str(image_root))}\0{os.path.normcase(str(workspace))}"
    ).encode()
    return f"lib-{hashlib.sha256(identity).hexdigest()[:20]}"


def _library_name(value: str | None, image_root: Path) -> str:
    if value is None:
        return image_root.name or "默认图库"
    return _required_text(value, "name")


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesktopConfigurationError(f"{name} 不能为空。")
    normalized = value.strip()
    if len(normalized) > 128 or any(
        character in "\r\n\x00" for character in normalized
    ):
        raise DesktopConfigurationError(f"{name} 格式无效。")
    return normalized


def _absolute_host_path(value: str | Path, label: str) -> Path:
    """Return a normalized host path while rejecting relative/container paths."""

    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise DesktopConfigurationError(
            f"{label} must be a valid absolute host path."
        ) from exc
    if not path.is_absolute():
        raise DesktopConfigurationError(
            f"{label} must be an absolute Windows host path."
        )
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise DesktopConfigurationError(f"Unable to resolve {label}.") from exc


def _existing_absolute_directory(value: str | Path, label: str) -> Path:
    path = _absolute_host_path(value, label)
    try:
        resolved = path.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise DesktopConfigurationError(
            f"{label} does not exist or is not accessible."
        ) from exc
    if not resolved.is_dir():
        raise DesktopConfigurationError(f"{label} must be a directory.")
    return resolved


def _ensure_absolute_directory(value: str | Path, label: str) -> Path:
    return _ensure_directory(_absolute_host_path(value, label))


def _ensure_directory(value: Path) -> Path:
    try:
        value.mkdir(parents=True, exist_ok=True)
        path = value.resolve(strict=True)
    except OSError as exc:
        raise DesktopConfigurationError(f"无法创建文件夹：{value}") from exc
    if not path.is_dir():
        raise DesktopConfigurationError(f"路径不是文件夹：{path}")
    return path


def _reject_reparse_point(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DesktopConfigurationError(f"无法安全检查路径：{path}") from exc
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_attribute):
        raise DesktopConfigurationError(f"配置路径不能是链接或重解析点：{path}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DesktopConfigurationError(f"config.json 包含重复字段：{key}")
        result[key] = value
    return result


__all__ = [
    "ConfigurationSnapshot",
    "DesktopConfigurationError",
    "DesktopConfigurationService",
]
