"""Desktop-facing facade for the user-editable Alibaba Cloud model catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from image_vector_service.model_catalog import (
    AUTO_TAG_ESCALATION_ROLE,
    AUTO_TAG_PRIMARY_ROLE,
    EMBEDDING_ROLE,
    ModelConfiguration,
    ModelConfigurationError,
    ModelDefinition,
    ensure_user_model_configuration,
    load_model_configuration,
    parse_model_configuration,
    resolve_user_model_config_path,
    save_model_configuration,
)


class ModelSettingsError(RuntimeError):
    """A model configuration could not be loaded, validated, or saved."""


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """One enabled model that can be assigned to a desktop role."""

    model_id: str
    display_name: str
    protocol: str
    roles: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelSettingsSnapshot:
    """Validated settings and role-specific dropdown choices for the UI."""

    path: Path
    provider: str
    embedding_model: str
    auto_tag_primary_model: str
    auto_tag_escalation_model: str
    embedding_choices: tuple[ModelChoice, ...]
    auto_tag_primary_choices: tuple[ModelChoice, ...]
    auto_tag_escalation_choices: tuple[ModelChoice, ...]
    json_text: str


class ModelSettingsService:
    """Load and atomically update ``models.json`` without storing credentials."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = (
            Path(path).expanduser().absolute()
            if path is not None
            else resolve_user_model_config_path()
        )

    @property
    def path(self) -> Path:
        return self._path

    def load(self, *, create: bool = True) -> ModelSettingsSnapshot:
        """Read settings, optionally materialising the safe default JSON file."""

        try:
            if create:
                ensure_user_model_configuration(self._path)
            configuration = load_model_configuration(self._path, missing_ok=not create)
        except (OSError, ModelConfigurationError) as exc:
            raise ModelSettingsError(str(exc)) from exc
        return _snapshot(self._path, configuration)

    def assign_roles(
        self,
        *,
        embedding_model: str,
        auto_tag_primary_model: str,
        auto_tag_escalation_model: str,
    ) -> ModelSettingsSnapshot:
        """Assign existing enabled models to all three supported roles."""

        current = self._load_configuration()
        payload = current.to_dict()
        payload["roles"] = {
            EMBEDDING_ROLE: _required_model_id(embedding_model, EMBEDDING_ROLE),
            AUTO_TAG_PRIMARY_ROLE: _required_model_id(
                auto_tag_primary_model, AUTO_TAG_PRIMARY_ROLE
            ),
            AUTO_TAG_ESCALATION_ROLE: _required_model_id(
                auto_tag_escalation_model, AUTO_TAG_ESCALATION_ROLE
            ),
        }
        return self._validate_save_snapshot(payload)

    def replace_json(self, json_text: str) -> ModelSettingsSnapshot:
        """Validate raw JSON from the advanced editor, then save atomically."""

        if not isinstance(json_text, str) or not json_text.strip():
            raise ModelSettingsError("模型配置 JSON 不能为空。")
        if len(json_text.encode("utf-8")) > 1024 * 1024:
            raise ModelSettingsError("模型配置 JSON 不能超过 1 MiB。")
        try:
            payload = json.loads(json_text, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, UnicodeError, ModelSettingsError) as exc:
            if isinstance(exc, ModelSettingsError):
                raise
            raise ModelSettingsError(f"模型配置不是有效 JSON：{exc}") from exc
        return self._validate_save_snapshot(payload)

    def _load_configuration(self) -> ModelConfiguration:
        try:
            ensure_user_model_configuration(self._path)
            return load_model_configuration(self._path)
        except (OSError, ModelConfigurationError) as exc:
            raise ModelSettingsError(str(exc)) from exc

    def _validate_save_snapshot(self, payload: Any) -> ModelSettingsSnapshot:
        try:
            configuration = parse_model_configuration(payload)
            save_model_configuration(self._path, configuration)
        except (OSError, ModelConfigurationError) as exc:
            raise ModelSettingsError(str(exc)) from exc
        return _snapshot(self._path, configuration)


def _snapshot(path: Path, configuration: ModelConfiguration) -> ModelSettingsSnapshot:
    choices = tuple(_choice(model) for model in configuration.models if model.enabled)
    embedding = tuple(choice for choice in choices if EMBEDDING_ROLE in choice.roles)
    primary = tuple(
        choice for choice in choices if AUTO_TAG_PRIMARY_ROLE in choice.roles
    )
    escalation = tuple(
        choice for choice in choices if AUTO_TAG_ESCALATION_ROLE in choice.roles
    )
    return ModelSettingsSnapshot(
        path=path,
        provider=configuration.provider,
        embedding_model=configuration.embedding_model,
        auto_tag_primary_model=configuration.auto_tag_primary_model,
        auto_tag_escalation_model=configuration.auto_tag_escalation_model,
        embedding_choices=embedding,
        auto_tag_primary_choices=primary,
        auto_tag_escalation_choices=escalation,
        json_text=(
            json.dumps(configuration.to_dict(), ensure_ascii=False, indent=2) + "\n"
        ),
    )


def _choice(model: ModelDefinition) -> ModelChoice:
    return ModelChoice(
        model_id=model.model_id,
        display_name=model.display_name,
        protocol=model.protocol,
        roles=model.roles,
    )


def _required_model_id(value: str, role: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelSettingsError(f"{role} 必须选择模型。")
    return value.strip()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelSettingsError(f"模型配置包含重复字段：{key}")
        result[key] = value
    return result


__all__ = [
    "ModelChoice",
    "ModelSettingsError",
    "ModelSettingsService",
    "ModelSettingsSnapshot",
]
