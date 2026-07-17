from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .model_catalog import (
    AUTO_TAG_ESCALATION_ROLE,
    AUTO_TAG_PRIMARY_ROLE,
    EMBEDDING_ROLE,
    ModelConfiguration,
    ModelConfigurationError,
    default_model_configuration,
)

DEFAULT_API_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)


class RuntimeCredentials:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._api_key = ""
        self._api_url: str | None = None

    def configure(self, api_key: str, api_url: str | None = None) -> None:
        with self._lock:
            self._api_key = api_key
            self._api_url = api_url

    @property
    def api_key(self) -> str:
        with self._lock:
            return self._api_key

    @property
    def api_url(self) -> str | None:
        with self._lock:
            return self._api_url


def _default_workspace() -> Path:
    configured = os.getenv("ZVEC_IMAGE_WORKSPACE")
    return (
        Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()
    )


@dataclass(frozen=True)
class ServiceConfig:
    workspace: Path = field(default_factory=_default_workspace)
    # Desktop library workers set this to keep annotation jobs inside the
    # selected library root even when a migrated Workspace retains old roots.
    # Standalone CLI services leave it unset and keep the historical all-roots
    # behavior.
    library_image_root: Path | None = None
    model_configuration: ModelConfiguration = field(
        default_factory=default_model_configuration
    )
    dimension: int = 1024
    metric: str = "COSINE"
    batch_size: int = 5
    timeout_seconds: int = 120
    max_retries: int = 3
    retry_base_seconds: float = 1.0
    max_top_k: int = 1000
    # Outbound data-URI ceiling. Keep a small margin below DashScope's 10 MiB
    # per-item hard limit so JSON envelope accounting cannot push it over.
    max_image_bytes: int = 10 * 1024 * 1024 - 64 * 1024
    # Independent local safety ceiling. Sources above the outbound limit are
    # decoded and compressed in memory instead of being rejected immediately.
    max_source_image_bytes: int = 256 * 1024 * 1024
    max_request_bytes: int = 50 * 1024 * 1024
    scan_concurrency: int = 4
    embedding_concurrency: int = 2
    auto_tag_concurrency: int = 2
    max_inflight_request_bytes: int = 96 * 1024 * 1024
    rate_limit_requests_per_minute: int = 48
    rate_limit_tokens_per_minute: int = 80_000
    rate_limit_hard_requests_per_minute: int = 60
    rate_limit_hard_tokens_per_minute: int = 100_000
    rate_limit_max_concurrency: int = 6
    embedding_estimated_tokens_per_image: int = 1_000
    embedding_estimated_tokens_per_text: int = 256
    results_directory: Path | None = None
    runtime_credentials: RuntimeCredentials | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def collection_path(self) -> Path:
        return self.workspace / "image_collection"

    @property
    def model(self) -> str:
        return self.model_configuration.embedding_model

    @property
    def auto_tag_primary_model(self) -> str:
        return self.model_configuration.auto_tag_primary_model

    @property
    def auto_tag_escalation_model(self) -> str:
        return self.model_configuration.auto_tag_escalation_model

    @property
    def collection_meta_path(self) -> Path:
        return self.workspace / "image_collection.meta.json"

    @property
    def state_path(self) -> Path:
        return self.workspace / "image_collection.state.sqlite3"

    @property
    def legacy_state_path(self) -> Path:
        return self.workspace / "image_collection.state.json"

    @property
    def lock_path(self) -> Path:
        return self.workspace / ".image_collection.lock"

    @property
    def results_path(self) -> Path:
        if self.results_directory is not None:
            return self.results_directory.expanduser().resolve()
        configured = os.getenv("ZVEC_IMAGE_RESULTS_DIR")
        return (
            Path(configured).expanduser().resolve()
            if configured
            else self.workspace / "search_results"
        )

    @property
    def log_dir(self) -> Path:
        configured = os.getenv("ZVEC_IMAGE_LOG_DIR")
        return (
            Path(configured).expanduser().resolve()
            if configured
            else self.workspace / "logs"
        )

    @property
    def api_url(self) -> str:
        if self.runtime_credentials is not None:
            configured = self.runtime_credentials.api_url
            if configured:
                return configured
        return os.getenv("DASHSCOPE_API_URL", DEFAULT_API_URL)

    @property
    def api_key(self) -> str:
        if self.runtime_credentials is not None:
            configured = self.runtime_credentials.api_key.strip()
            if configured:
                return configured
        return os.getenv("DASHSCOPE_API_KEY", "").strip()

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigurationError(
                "DASHSCOPE_API_KEY is not set. Configure it as an environment variable."
            )
        return self.api_key

    def validate(self) -> None:
        try:
            embedding = self.model_configuration.model_for_role(EMBEDDING_ROLE)
            self.model_configuration.model_for_role(AUTO_TAG_PRIMARY_ROLE)
            self.model_configuration.model_for_role(AUTO_TAG_ESCALATION_ROLE)
        except ModelConfigurationError as exc:
            raise ConfigurationError(str(exc)) from exc
        if embedding.dimension != self.dimension:
            raise ConfigurationError(
                "The selected embedding model dimension does not match the "
                f"Collection dimension ({embedding.dimension} != {self.dimension})."
            )
        if self.dimension != 1024:
            raise ConfigurationError(
                "This collection requires 1024-dimensional vectors."
            )
        if self.metric != "COSINE":
            raise ConfigurationError("This collection requires COSINE similarity.")
        if not 1 <= self.batch_size <= 5:
            raise ConfigurationError("batch_size must be between 1 and 5.")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("timeout_seconds must be positive.")
        if self.max_retries < 0:
            raise ConfigurationError("max_retries cannot be negative.")
        if self.max_top_k < 1:
            raise ConfigurationError("max_top_k must be positive.")
        for name in ("max_image_bytes", "max_source_image_bytes"):
            if getattr(self, name) < 1:
                raise ConfigurationError(f"{name} must be positive.")
        if self.max_request_bytes < self.max_image_bytes:
            raise ConfigurationError(
                "max_request_bytes must be greater than or equal to max_image_bytes."
            )
        for name in (
            "scan_concurrency",
            "embedding_concurrency",
            "auto_tag_concurrency",
        ):
            value = getattr(self, name)
            if not 1 <= value <= 32:
                raise ConfigurationError(f"{name} must be between 1 and 32.")
        if not self.max_request_bytes <= self.max_inflight_request_bytes <= 2**30:
            raise ConfigurationError(
                "max_inflight_request_bytes must be at least max_request_bytes "
                "and no greater than 1 GiB."
            )
        rate_limit_fields = (
            "rate_limit_requests_per_minute",
            "rate_limit_tokens_per_minute",
            "rate_limit_hard_requests_per_minute",
            "rate_limit_hard_tokens_per_minute",
            "rate_limit_max_concurrency",
            "embedding_estimated_tokens_per_image",
            "embedding_estimated_tokens_per_text",
        )
        for name in rate_limit_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigurationError(f"{name} must be a positive integer.")
        if (
            self.rate_limit_requests_per_minute
            > self.rate_limit_hard_requests_per_minute
        ):
            raise ConfigurationError(
                "rate_limit_requests_per_minute cannot exceed its hard limit."
            )
        if self.rate_limit_tokens_per_minute > self.rate_limit_hard_tokens_per_minute:
            raise ConfigurationError(
                "rate_limit_tokens_per_minute cannot exceed its hard limit."
            )
        if self.embedding_concurrency > self.rate_limit_max_concurrency:
            raise ConfigurationError(
                "embedding_concurrency cannot exceed rate_limit_max_concurrency."
            )
        if self.auto_tag_concurrency > self.rate_limit_max_concurrency:
            raise ConfigurationError(
                "auto_tag_concurrency cannot exceed rate_limit_max_concurrency."
            )


class ConfigurationError(RuntimeError):
    pass
