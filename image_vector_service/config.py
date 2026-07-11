from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_API_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "multimodal-embedding/multimodal-embedding"
)


@dataclass(frozen=True)
class ServiceConfig:
    workspace: Path = Path(r"D:\code\zvec")
    model: str = "qwen3-vl-embedding"
    dimension: int = 1024
    metric: str = "COSINE"
    batch_size: int = 5
    timeout_seconds: int = 120
    max_retries: int = 3
    retry_base_seconds: float = 1.0

    @property
    def collection_path(self) -> Path:
        return self.workspace / "image_collection"

    @property
    def collection_meta_path(self) -> Path:
        return self.workspace / "image_collection.meta.json"

    @property
    def state_path(self) -> Path:
        return self.workspace / "image_collection.state.json"

    @property
    def results_path(self) -> Path:
        return self.workspace / "search_results"

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
            raise ConfigurationError("This collection requires 1024-dimensional vectors.")
        if self.metric != "COSINE":
            raise ConfigurationError("This collection requires COSINE similarity.")
        if not 1 <= self.batch_size <= 5:
            raise ConfigurationError("batch_size must be between 1 and 5.")
        if self.timeout_seconds <= 0:
            raise ConfigurationError("timeout_seconds must be positive.")
        if self.max_retries < 0:
            raise ConfigurationError("max_retries cannot be negative.")


class ConfigurationError(RuntimeError):
    pass
