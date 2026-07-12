from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_API_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)


def _default_workspace() -> Path:
    configured = os.getenv("ZVEC_IMAGE_WORKSPACE")
    return (
        Path(configured).expanduser().resolve() if configured else Path.cwd().resolve()
    )


@dataclass(frozen=True)
class ServiceConfig:
    workspace: Path = field(default_factory=_default_workspace)
    model: str = "qwen3-vl-embedding"
    dimension: int = 1024
    metric: str = "COSINE"
    batch_size: int = 5
    timeout_seconds: int = 120
    max_retries: int = 3
    retry_base_seconds: float = 1.0
    max_top_k: int = 1000
    max_image_bytes: int = 20 * 1024 * 1024
    max_request_bytes: int = 50 * 1024 * 1024

    @property
    def collection_path(self) -> Path:
        return self.workspace / "image_collection"

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
        return os.getenv("DASHSCOPE_API_URL", DEFAULT_API_URL)

    @property
    def api_key(self) -> str:
        return os.getenv("DASHSCOPE_API_KEY", "").strip()

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigurationError(
                "DASHSCOPE_API_KEY is not set. Configure it as an environment variable."
            )
        return self.api_key

    def validate(self) -> None:
        if self.model != "qwen3-vl-embedding":
            raise ConfigurationError("This collection requires qwen3-vl-embedding.")
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
        if self.max_image_bytes < 1:
            raise ConfigurationError("max_image_bytes must be positive.")
        if self.max_request_bytes < self.max_image_bytes:
            raise ConfigurationError(
                "max_request_bytes must be greater than or equal to max_image_bytes."
            )


class ConfigurationError(RuntimeError):
    pass
