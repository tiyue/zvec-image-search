from __future__ import annotations

import base64
import json
import math
import random
import time
import urllib.error
import urllib.request
from contextlib import suppress
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
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        code: str = "",
        splittable: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.splittable = splittable


class ImageInputError(ValueError):
    splittable = True


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
                raise ImageInputError(f"Unsupported image extension: {extension}")
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise ImageInputError(f"Cannot read image {path}: {exc}") from exc
            if size > self.config.max_image_bytes:
                raise ImageInputError(
                    f"Image {path} exceeds {self.config.max_image_bytes} bytes."
                )
            try:
                _format, mime_type, _width, _height = inspect_image_content(path)
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except Exception as exc:
                raise ImageInputError(f"Cannot encode image {path}: {exc}") from exc
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
                    f"Expected vector dimension {self.config.dimension}, "
                    f"received {actual}."
                )
            converted = [float(value) for value in vector]
            if not all(math.isfinite(value) for value in converted):
                raise DashScopeError("Embedding contains NaN or infinite values.")
            vectors.append(converted)

        return EmbeddingResponse(
            vectors=vectors,
            request_id=str(body.get("request_id") or output.get("request_id") or ""),
            usage=dict(body.get("usage") or {}),
        )

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(request_data) > self.config.max_request_bytes:
            raise ImageInputError(
                f"Request payload exceeds {self.config.max_request_bytes} bytes."
            )
        last_error: Exception | None = None

        for attempt in range(self.config.max_retries + 1):
            retry_after: str | None = None
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
                code, message = self._safe_error_details(exc)
                splittable = self._is_splittable_input_error(status, code, message)
                last_error = DashScopeError(
                    f"DashScope {code or 'HTTPError'}: {message}",
                    status_code=status,
                    code=code,
                    splittable=splittable,
                )
                if status not in {408, 409, 429, 500, 502, 503, 504}:
                    raise last_error from exc
                retry_after = exc.headers.get("Retry-After")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = DashScopeError(f"DashScope request failed: {exc}")
                retry_after = None

            if attempt < self.config.max_retries:
                delay = self.config.retry_base_seconds * (2**attempt)
                if retry_after:
                    with suppress(ValueError):
                        delay = max(delay, float(retry_after))
                time.sleep(delay + random.uniform(0, delay * 0.2))

        assert last_error is not None
        raise last_error

    @staticmethod
    def _safe_error_details(error: urllib.error.HTTPError) -> tuple[str, str]:
        try:
            body = json.loads(error.read().decode("utf-8"))
            code = str(body.get("code") or "")
            message = str(body.get("message") or error.reason)
            return code, message
        except Exception:
            return "", str(error.reason)

    @staticmethod
    def _is_splittable_input_error(status: int, code: str, message: str) -> bool:
        if status not in {400, 413, 422}:
            return False
        text = f"{code} {message}".lower()
        return any(
            token in text
            for token in ("image", "media", "file", "content", "base64", "format")
        )
