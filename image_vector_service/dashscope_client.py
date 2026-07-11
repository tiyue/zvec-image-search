from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ServiceConfig
from .image_scanner import SUPPORTED_EXTENSIONS, inspect_image_content


@dataclass(frozen=True)
class EmbeddingResponse:
    vectors: list[list[float]]
    request_id: str
    usage: dict[str, Any]


class DashScopeError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DashScopeEmbeddingClient:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self.config.validate()
        self.api_key = config.require_api_key()
        self.request_count = 0

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        if not image_paths:
            return EmbeddingResponse([], "", {})
        if len(image_paths) > self.config.batch_size:
            raise ValueError(
                f"A batch can contain at most {self.config.batch_size} images."
            )

        contents = []
        for path in image_paths:
            extension = path.suffix.lower()
            if extension not in SUPPORTED_EXTENSIONS:
                raise ValueError(f"Unsupported image extension: {extension}")
            _format, mime_type, _width, _height = inspect_image_content(path)
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            contents.append({"image": f"data:{mime_type};base64,{encoded}"})
        return self._embed(contents)

    def embed_text(self, text: str) -> EmbeddingResponse:
        text = text.strip()
        if not text:
            raise ValueError("Search text cannot be empty.")
        return self._embed([{"text": text}])

    def _embed(self, contents: list[dict[str, str]]) -> EmbeddingResponse:
        payload = {
            "model": self.config.model,
            "input": {"contents": contents},
            "parameters": {
                "output_type": "dense",
                "dimension": self.config.dimension,
                "enable_fusion": False,
            },
        }
        body = self._post_json(payload)
        output = body.get("output") or {}
        embeddings = output.get("embeddings") or []
        if len(embeddings) != len(contents):
            raise DashScopeError(
                f"Expected {len(contents)} embeddings, received {len(embeddings)}."
            )

        embeddings = sorted(embeddings, key=lambda item: int(item.get("index", 0)))
        vectors: list[list[float]] = []
        for item in embeddings:
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) != self.config.dimension:
                actual = len(vector) if isinstance(vector, list) else "unknown"
                raise DashScopeError(
                    f"Expected vector dimension {self.config.dimension}, received {actual}."
                )
            vectors.append([float(value) for value in vector])

        return EmbeddingResponse(
            vectors=vectors,
            request_id=str(body.get("request_id") or output.get("request_id") or ""),
            usage=dict(body.get("usage") or {}),
        )

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            request = urllib.request.Request(
                self.config.api_url,
                data=request_data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "zvec-local-image-service/1.0",
                },
            )
            self.request_count += 1
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                status = exc.code
                message = self._safe_error_message(exc)
                last_error = DashScopeError(message, status)
                if status not in {408, 409, 429, 500, 502, 503, 504}:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = DashScopeError(f"DashScope request failed: {exc}")

            if attempt < self.config.max_retries:
                time.sleep(self.config.retry_base_seconds * (2**attempt))

        assert last_error is not None
        raise last_error

    @staticmethod
    def _safe_error_message(error: urllib.error.HTTPError) -> str:
        try:
            body = json.loads(error.read().decode("utf-8"))
            code = body.get("code") or "HTTPError"
            message = body.get("message") or error.reason
            return f"DashScope {code}: {message}"
        except Exception:
            return f"DashScope HTTP {error.code}: {error.reason}"
