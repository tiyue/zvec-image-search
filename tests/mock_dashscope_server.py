from __future__ import annotations

import hashlib
import json
import math
import os
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DIMENSION = int(os.environ.get("MOCK_EMBEDDING_DIMENSION", "1024"))
PORT = int(os.environ.get("MOCK_DASHSCOPE_PORT", "8000"))
EXPECTED_AUTHORIZATION = "Bearer test-key-not-real"


def _embedding(content: dict[str, Any]) -> list[float]:
    payload = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    values = [
        (((digest[index % len(digest)] + index * 17) % 255) - 127) / 127
        for index in range(DIMENSION)
    ]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


class Handler(BaseHTTPRequestHandler):
    server_version = "ZvecMockDashScope/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._write_json(200, {"status": "ok"})
            return
        self._write_json(404, {"code": "NotFound", "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != EXPECTED_AUTHORIZATION:
            self._write_json(
                401, {"code": "Unauthorized", "message": "invalid authorization"}
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            contents = payload["input"]["contents"]
            if not isinstance(contents, list):
                raise TypeError("contents must be a list")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self._write_json(400, {"code": "InvalidRequest", "message": str(exc)})
            return

        if any(item.get("text") == "__slow__" for item in contents):
            print("mock-slow-request", flush=True)
            time.sleep(30)

        embeddings = [
            {"index": index, "embedding": _embedding(content)}
            for index, content in enumerate(contents)
        ]
        self._write_json(
            200,
            {
                "output": {"embeddings": embeddings},
                "request_id": "mock-request",
                "usage": {"input_count": len(contents)},
            },
        )

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print("mock-ready", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
