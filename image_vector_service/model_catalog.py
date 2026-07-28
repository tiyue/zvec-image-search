from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

MODEL_CONFIG_SCHEMA_VERSION: Final = 1
MODEL_PROVIDER: Final = "aliyun_dashscope"
EMBEDDING_ROLE: Final = "embedding"
AUTO_TAG_PRIMARY_ROLE: Final = "auto_tag_primary"
AUTO_TAG_ESCALATION_ROLE: Final = "auto_tag_escalation"
MODEL_ROLES: Final = frozenset(
    {EMBEDDING_ROLE, AUTO_TAG_PRIMARY_ROLE, AUTO_TAG_ESCALATION_ROLE}
)
EMBEDDING_PROTOCOL: Final = "dashscope_multimodal_embedding"
CONVERSATION_PROTOCOL: Final = "dashscope_multimodal_conversation"
MODEL_PROTOCOLS: Final = frozenset({EMBEDDING_PROTOCOL, CONVERSATION_PROTOCOL})
DEFAULT_MODEL_CATALOG_FILE: Final = "model-catalog.default.json"
USER_MODEL_CONFIG_FILE: Final = "models.json"
MODEL_CONCURRENCY_OPTIONS: Final = (1, 2, 4, 6)
DEFAULT_EMBEDDING_CONCURRENCY: Final = 2
DEFAULT_AUTO_TAG_CONCURRENCY: Final = 4

_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_MODELS = 100
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
_EFFECTIVE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "provider",
        "models",
        "roles",
        "embedding_concurrency",
        "auto_tag_concurrency",
    }
)
_MODEL_FIELDS = frozenset(
    {"id", "display_name", "roles", "protocol", "dimension", "enabled", "pricing"}
)
_PRICING_FIELDS = frozenset(
    {"input_yuan_per_million", "output_yuan_per_million", "effective_from"}
)
_LEGACY_MODEL_ALIASES = {
    "flash": "qwen3-vl-flash",
    "plus": "qwen3-vl-plus",
}


class ModelConfigurationError(ValueError):
    """Raised when the user-editable Alibaba Cloud model catalog is unsafe."""


# Keep the implementation readable without importing ServiceConfig and creating a
# module cycle. Callers may catch ModelConfigurationError or the existing ValueError
# configuration boundary used by the CLI/backend.
ConfigurationError = ModelConfigurationError


@dataclass(frozen=True)
class ModelPricing:
    input_yuan_per_million: float
    output_yuan_per_million: float
    effective_from: str

    def to_dict(self) -> dict[str, float | str]:
        return {
            "input_yuan_per_million": self.input_yuan_per_million,
            "output_yuan_per_million": self.output_yuan_per_million,
            "effective_from": self.effective_from,
        }


@dataclass(frozen=True)
class ModelDefinition:
    model_id: str
    display_name: str
    roles: tuple[str, ...]
    protocol: str
    dimension: int | None
    enabled: bool
    pricing: ModelPricing | None

    def supports(self, role: str) -> bool:
        return self.enabled and role in self.roles

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.model_id,
            "display_name": self.display_name,
            "roles": list(self.roles),
            "protocol": self.protocol,
        }
        if self.dimension is not None:
            payload["dimension"] = self.dimension
        payload["enabled"] = self.enabled
        if self.pricing is not None:
            payload["pricing"] = self.pricing.to_dict()
        return payload


@dataclass(frozen=True)
class ModelConfiguration:
    provider: str
    models: tuple[ModelDefinition, ...]
    role_models: tuple[tuple[str, str], ...]
    embedding_concurrency: int = DEFAULT_EMBEDDING_CONCURRENCY
    auto_tag_concurrency: int = DEFAULT_AUTO_TAG_CONCURRENCY

    @property
    def by_id(self) -> dict[str, ModelDefinition]:
        return {model.model_id: model for model in self.models}

    @property
    def roles(self) -> dict[str, str]:
        return dict(self.role_models)

    @property
    def embedding_model(self) -> str:
        return self.roles[EMBEDDING_ROLE]

    @property
    def auto_tag_primary_model(self) -> str:
        return self.roles[AUTO_TAG_PRIMARY_ROLE]

    @property
    def auto_tag_escalation_model(self) -> str:
        return self.roles[AUTO_TAG_ESCALATION_ROLE]

    def model_for_role(self, role: str) -> ModelDefinition:
        if role not in MODEL_ROLES:
            raise ConfigurationError(f"Unknown model role: {role}")
        model_id = self.roles[role]
        model = self.by_id[model_id]
        if not model.supports(role):
            raise ConfigurationError(
                f"Model {model_id!r} is not enabled for role {role!r}."
            )
        return model

    def resolve_auto_tag_model(self, value: str | None) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return self.auto_tag_primary_model
        normalized = _LEGACY_MODEL_ALIASES.get(normalized.lower(), normalized)
        model = self.by_id.get(normalized)
        if model is None or not model.enabled:
            raise ConfigurationError(
                f"Unknown or disabled Alibaba Cloud visual model: {normalized}"
            )
        if not (
            model.supports(AUTO_TAG_PRIMARY_ROLE)
            or model.supports(AUTO_TAG_ESCALATION_ROLE)
        ):
            raise ConfigurationError(
                f"Model {normalized!r} cannot be used for visual auto-tagging."
            )
        return model.model_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_CONFIG_SCHEMA_VERSION,
            "provider": self.provider,
            "models": [model.to_dict() for model in self.models],
            "roles": self.roles,
            "embedding_concurrency": self.embedding_concurrency,
            "auto_tag_concurrency": self.auto_tag_concurrency,
        }


def default_model_configuration() -> ModelConfiguration:
    return parse_model_configuration(_default_payload())


def resolve_user_model_config_path() -> Path:
    explicit = os.getenv("ZVEC_MODELS_CONFIG", "").strip()
    if explicit:
        return _absolute_path(explicit)
    configured_home = (
        os.getenv("ZVEC_CONFIG_HOME", "").strip()
        or os.getenv("ZVEC_DOCKER_CONFIG_HOME", "").strip()
    )
    if configured_home:
        return _absolute_path(configured_home) / USER_MODEL_CONFIG_FILE
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return (
            _absolute_path(os.environ["LOCALAPPDATA"])
            / "zvec-image-search"
            / USER_MODEL_CONFIG_FILE
        )
    return _absolute_path(Path.home()) / ".zvec-image-search" / USER_MODEL_CONFIG_FILE


def load_active_model_configuration() -> ModelConfiguration:
    path = resolve_user_model_config_path()
    return load_model_configuration(path, missing_ok=True)


def load_model_configuration(
    path: str | Path,
    *,
    missing_ok: bool = False,
) -> ModelConfiguration:
    config_path = _absolute_path(path)
    if _is_reparse_point(config_path):
        raise ConfigurationError(
            f"Model configuration must not be a symbolic link or reparse point: "
            f"{config_path}"
        )
    if not config_path.exists():
        if missing_ok:
            return default_model_configuration()
        raise ConfigurationError(f"Model configuration is missing: {config_path}")
    if not config_path.is_file():
        raise ConfigurationError(
            f"Model configuration must be a regular file: {config_path}"
        )
    try:
        size = config_path.stat().st_size
        if size <= 0 or size > _MAX_CONFIG_BYTES:
            raise ConfigurationError(
                "Model configuration must be between 1 byte and 1 MiB."
            )
        raw = config_path.read_bytes()
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except ConfigurationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            f"Model configuration is not valid UTF-8 JSON: {config_path}"
        ) from exc
    return parse_model_configuration(payload)


def ensure_user_model_configuration(path: str | Path | None = None) -> Path:
    config_path = (
        _absolute_path(path) if path is not None else resolve_user_model_config_path()
    )
    if _is_reparse_point(config_path):
        raise ConfigurationError(
            "Refusing to use a symbolic-link or reparse-point model config."
        )
    if config_path.exists():
        load_model_configuration(config_path)
        return config_path
    save_model_configuration(config_path, default_model_configuration())
    return config_path


def save_model_configuration(
    path: str | Path,
    configuration: ModelConfiguration,
) -> None:
    # Serializing through the parser keeps programmatic callers on the same strict
    # contract as hand-edited JSON before anything reaches disk.
    validated = parse_model_configuration(configuration.to_dict())
    config_path = _absolute_path(path)
    if _is_reparse_point(config_path):
        raise ConfigurationError(
            "Refusing to replace a symbolic-link or reparse-point model config."
        )
    if config_path.exists() and not config_path.is_file():
        raise ConfigurationError(
            f"Model configuration must be a regular file: {config_path}"
        )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(validated.to_dict(), ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(config_path)
        if os.name != "nt":
            config_path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def parse_model_configuration(payload: Any) -> ModelConfiguration:
    if not isinstance(payload, dict):
        raise ConfigurationError("Model configuration must be a JSON object.")
    _reject_unknown_fields(payload, _ROOT_FIELDS, "model configuration")
    if payload.get("schema_version") != MODEL_CONFIG_SCHEMA_VERSION:
        raise ConfigurationError(
            f"Model configuration schema_version must be {MODEL_CONFIG_SCHEMA_VERSION}."
        )
    if payload.get("provider") != MODEL_PROVIDER:
        raise ConfigurationError(
            f"Model provider must be {MODEL_PROVIDER!r}; "
            "only Alibaba Cloud is supported."
        )
    raw_models = payload.get("models")
    if not isinstance(raw_models, list) or not 1 <= len(raw_models) <= _MAX_MODELS:
        raise ConfigurationError("models must contain between 1 and 100 items.")
    models = tuple(_parse_model(item, index) for index, item in enumerate(raw_models))
    by_id: dict[str, ModelDefinition] = {}
    for model in models:
        if model.model_id in by_id:
            raise ConfigurationError(f"Duplicate model id: {model.model_id}")
        by_id[model.model_id] = model

    raw_roles = payload.get("roles")
    if not isinstance(raw_roles, dict):
        raise ConfigurationError("roles must be an object.")
    _reject_unknown_fields(raw_roles, MODEL_ROLES, "roles")
    if set(raw_roles) != set(MODEL_ROLES):
        missing = sorted(set(MODEL_ROLES) - set(raw_roles))
        raise ConfigurationError(f"roles is missing: {', '.join(missing)}")
    role_models: list[tuple[str, str]] = []
    for role in sorted(MODEL_ROLES):
        model_id = _required_text(raw_roles.get(role), f"roles.{role}", 128)
        selected_model = by_id.get(model_id)
        if selected_model is None:
            raise ConfigurationError(
                f"roles.{role} references unknown model {model_id!r}."
            )
        if not selected_model.supports(role):
            raise ConfigurationError(
                f"Model {model_id!r} is not enabled for roles.{role}."
            )
        _validate_role_protocol(role, selected_model)
        if role != EMBEDDING_ROLE and selected_model.pricing is None:
            raise ConfigurationError(
                f"Selected visual model {model_id!r} requires pricing metadata."
            )
        role_models.append((role, model_id))
    embedding_concurrency = _model_concurrency(
        payload.get("embedding_concurrency", DEFAULT_EMBEDDING_CONCURRENCY),
        "embedding_concurrency",
    )
    auto_tag_concurrency = _model_concurrency(
        payload.get("auto_tag_concurrency", DEFAULT_AUTO_TAG_CONCURRENCY),
        "auto_tag_concurrency",
    )
    return ModelConfiguration(
        MODEL_PROVIDER,
        models,
        tuple(role_models),
        embedding_concurrency,
        auto_tag_concurrency,
    )


def _parse_model(payload: Any, index: int) -> ModelDefinition:
    label = f"models[{index}]"
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label} must be an object.")
    _reject_unknown_fields(payload, _MODEL_FIELDS, label)
    model_id = _required_text(payload.get("id"), f"{label}.id", 128)
    if not _MODEL_ID.fullmatch(model_id):
        raise ConfigurationError(f"{label}.id has an unsupported format.")
    display_name = _required_text(
        payload.get("display_name"), f"{label}.display_name", 128
    )
    raw_roles = payload.get("roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise ConfigurationError(f"{label}.roles must be a non-empty array.")
    roles: list[str] = []
    for value in raw_roles:
        role = _required_text(value, f"{label}.roles[]", 64)
        if role not in MODEL_ROLES:
            raise ConfigurationError(f"{label}.roles contains unknown role {role!r}.")
        if role in roles:
            raise ConfigurationError(f"{label}.roles contains duplicate {role!r}.")
        roles.append(role)
    protocol = _required_text(payload.get("protocol"), f"{label}.protocol", 80)
    if protocol not in MODEL_PROTOCOLS:
        raise ConfigurationError(f"{label}.protocol is not supported: {protocol}")
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigurationError(f"{label}.enabled must be a boolean.")
    raw_dimension = payload.get("dimension")
    if raw_dimension is None:
        dimension = None
    elif isinstance(raw_dimension, bool) or not isinstance(raw_dimension, int):
        raise ConfigurationError(f"{label}.dimension must be an integer or null.")
    elif not 1 <= raw_dimension <= 65_536:
        raise ConfigurationError(f"{label}.dimension is outside the supported range.")
    else:
        dimension = raw_dimension
    pricing = _parse_pricing(payload.get("pricing"), label)
    model = ModelDefinition(
        model_id,
        display_name,
        tuple(roles),
        protocol,
        dimension,
        enabled,
        pricing,
    )
    for role in roles:
        _validate_role_protocol(role, model)
    return model


def _parse_pricing(payload: Any, model_label: str) -> ModelPricing | None:
    if payload is None:
        return None
    label = f"{model_label}.pricing"
    if not isinstance(payload, dict):
        raise ConfigurationError(f"{label} must be an object or null.")
    _reject_unknown_fields(payload, _PRICING_FIELDS, label)
    if set(payload) != set(_PRICING_FIELDS):
        missing = sorted(set(_PRICING_FIELDS) - set(payload))
        raise ConfigurationError(f"{label} is missing: {', '.join(missing)}")
    input_price = _non_negative_number(
        payload.get("input_yuan_per_million"),
        f"{label}.input_yuan_per_million",
    )
    output_price = _non_negative_number(
        payload.get("output_yuan_per_million"),
        f"{label}.output_yuan_per_million",
    )
    effective_from = _required_text(
        payload.get("effective_from"), f"{label}.effective_from", 32
    )
    if not _EFFECTIVE_DATE.fullmatch(effective_from):
        raise ConfigurationError(f"{label}.effective_from must use YYYY-MM-DD.")
    try:
        date.fromisoformat(effective_from)
    except ValueError as exc:
        raise ConfigurationError(
            f"{label}.effective_from must be a valid calendar date."
        ) from exc
    return ModelPricing(input_price, output_price, effective_from)


def _validate_role_protocol(role: str, model: ModelDefinition) -> None:
    if role == EMBEDDING_ROLE:
        if model.protocol != EMBEDDING_PROTOCOL or model.dimension != 1024:
            raise ConfigurationError(
                f"Embedding model {model.model_id!r} must use {EMBEDDING_PROTOCOL!r} "
                "with dimension 1024 for the current Collection schema."
            )
        return
    if model.protocol != CONVERSATION_PROTOCOL or model.dimension is not None:
        raise ConfigurationError(
            f"Visual model {model.model_id!r} must use {CONVERSATION_PROTOCOL!r} "
            "and must not declare a vector dimension."
        )


def _reject_unknown_fields(
    payload: dict[str, Any], allowed: frozenset[str], label: str
) -> None:
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise ConfigurationError(
            f"{label} contains unsupported fields: {', '.join(unknown)}"
        )


def _required_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{label} must be a non-empty string.")
    normalized = value.strip()
    has_control_character = any(character in "\r\n\x00" for character in normalized)
    if len(normalized) > maximum or has_control_character:
        raise ConfigurationError(
            f"{label} is too long or contains a control character."
        )
    return normalized


def _non_negative_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{label} must be a non-negative number.")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ConfigurationError(f"{label} must be a finite non-negative number.")
    return normalized


def _model_concurrency(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in MODEL_CONCURRENCY_OPTIONS
    ):
        choices = ", ".join(str(option) for option in MODEL_CONCURRENCY_OPTIONS)
        raise ConfigurationError(f"{label} must be one of: {choices}.")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"Duplicate JSON property: {key}")
        result[key] = value
    return result


def _absolute_path(path: str | Path) -> Path:
    """Resolve the parent while preserving the final entry for an lstat check."""

    absolute = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    return absolute.parent.resolve(strict=False) / absolute.name


def _is_reparse_point(path: Path) -> bool:
    """Detect links, junctions, and Windows reparse points without following them."""

    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ConfigurationError(
            f"Model configuration path cannot be safely inspected: {path}"
        ) from exc
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_attribute)


def _default_payload() -> dict[str, Any]:
    return {
        "schema_version": MODEL_CONFIG_SCHEMA_VERSION,
        "provider": MODEL_PROVIDER,
        "models": [
            {
                "id": "qwen3-vl-embedding",
                "display_name": "Qwen3-VL Embedding",
                "roles": [EMBEDDING_ROLE],
                "protocol": EMBEDDING_PROTOCOL,
                "dimension": 1024,
                "enabled": True,
            },
            {
                "id": "qwen3-vl-flash",
                "display_name": "Qwen3-VL Flash",
                "roles": [AUTO_TAG_PRIMARY_ROLE, AUTO_TAG_ESCALATION_ROLE],
                "protocol": CONVERSATION_PROTOCOL,
                "enabled": True,
                "pricing": {
                    "input_yuan_per_million": 0.15,
                    "output_yuan_per_million": 1.5,
                    "effective_from": "2026-07-14",
                },
            },
            {
                "id": "qwen3-vl-plus",
                "display_name": "Qwen3-VL Plus",
                "roles": [AUTO_TAG_PRIMARY_ROLE, AUTO_TAG_ESCALATION_ROLE],
                "protocol": CONVERSATION_PROTOCOL,
                "enabled": True,
                "pricing": {
                    "input_yuan_per_million": 1.0,
                    "output_yuan_per_million": 10.0,
                    "effective_from": "2026-07-14",
                },
            },
        ],
        "roles": {
            EMBEDDING_ROLE: "qwen3-vl-embedding",
            AUTO_TAG_PRIMARY_ROLE: "qwen3-vl-flash",
            AUTO_TAG_ESCALATION_ROLE: "qwen3-vl-plus",
        },
        "embedding_concurrency": DEFAULT_EMBEDDING_CONCURRENCY,
        "auto_tag_concurrency": DEFAULT_AUTO_TAG_CONCURRENCY,
    }
