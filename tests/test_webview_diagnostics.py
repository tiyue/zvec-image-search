from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from zvec_webview.server import GatewayServer


class _Facade:
    def __init__(self, config_home: Path) -> None:
        self.config_home = config_home

    def bootstrap(self) -> dict[str, object]:
        return {"service": {"status": "ready", "backend_ready": True}}

    def settings(self) -> dict[str, object]:
        return {}

    def latest_results(self, *, page: int, page_size: int) -> dict[str, object]:
        return {"page": page, "page_size": page_size, "items": []}


def _write_frontend_build(root: Path) -> None:
    (root / "assets").mkdir(parents=True)
    (root / ".vite").mkdir()
    (root / "assets/app.js").write_text("window.ready = true;", encoding="utf-8")
    (root / "assets/app.css").write_text("body { color: #111; }", encoding="utf-8")
    (root / "index.html").write_text(
        '<script type="module" src="./assets/app.js"></script>'
        '<link rel="stylesheet" href="./assets/app.css">',
        encoding="utf-8",
    )
    (root / ".vite/manifest.json").write_text(
        json.dumps(
            {
                "index.html": {
                    "file": "assets/app.js",
                    "src": "index.html",
                    "isEntry": True,
                    "css": ["assets/app.css"],
                }
            }
        ),
        encoding="utf-8",
    )


def _post(
    url: str,
    body: bytes,
    *,
    content_type: str = "application/json",
    origin: str | None = None,
) -> tuple[int, dict[str, object]]:
    headers = {"Content-Type": content_type, "Accept": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    request = Request(url, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


class FrontendDiagnosticGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        assets = self.root / "assets"
        assets.mkdir()
        _write_frontend_build(assets)
        self.token = "diagnostic-token-" + "x" * 24
        self.facade = _Facade(self.root / "config-home")
        self.server = GatewayServer(
            self.facade,  # type: ignore[arg-type]
            assets_directory=assets,
            token=self.token,
        )
        self.server.start()
        self.endpoint = self.server.url + "api/diagnostics/frontend"

    def tearDown(self) -> None:
        self.server.stop()
        self.temporary.cleanup()

    def test_accepts_redacts_and_persists_one_safe_json_line(self) -> None:
        payload = {
            "event": "search_failed",
            "level": "error",
            "message": (
                f"request failed\r\nBearer abcdefghijklmnop {self.token} "
                "api_key=sk-secretvalue123 C:\\Users\\person\\private.jpg"
            ),
            "details": {
                "attempt": 3,
                "recoverable": True,
                "api_key": "sk-must-not-be-written",
            },
            "timestamp": "2026-07-19T12:30:00.000Z",
        }
        status, response = _post(
            self.endpoint,
            json.dumps(payload).encode("utf-8"),
        )

        self.assertEqual(status, 202)
        self.assertTrue(response["accepted"])
        self.assertTrue(response["persisted"])
        log_path = Path(str(response["log_file"]))
        self.assertEqual(
            log_path,
            self.facade.config_home / "logs" / "frontend-diagnostics.jsonl",
        )
        lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        written = json.loads(lines[0])
        self.assertEqual(written["event"], "search_failed")
        self.assertEqual(written["level"], "error")
        self.assertNotIn("\n", written["message"])
        self.assertNotIn(self.token, lines[0])
        self.assertNotIn("sk-secretvalue123", lines[0])
        self.assertNotIn("C:\\Users\\person", lines[0])
        self.assertEqual(written["details"]["api_key"], "<redacted>")
        self.assertIn("server_time", written)

    def test_rejects_invalid_json_and_schema(self) -> None:
        with self.assertRaises(HTTPError) as invalid_json:
            _post(self.endpoint, b"{")
        self.assertEqual(invalid_json.exception.code, 400)
        invalid_payload = json.loads(invalid_json.exception.read().decode("utf-8"))
        invalid_json.exception.close()
        self.assertEqual(invalid_payload["error"]["code"], "invalid_json")

        with self.assertRaises(HTTPError) as invalid_schema:
            _post(
                self.endpoint,
                json.dumps(
                    {
                        "event": "arbitrary_event",
                        "level": "error",
                        "message": "bad",
                    }
                ).encode("utf-8"),
            )
        self.assertEqual(invalid_schema.exception.code, 400)
        schema_payload = json.loads(invalid_schema.exception.read().decode("utf-8"))
        invalid_schema.exception.close()
        self.assertEqual(schema_payload["error"]["code"], "invalid_diagnostic")

    def test_rejects_payload_over_diagnostic_limit(self) -> None:
        body = json.dumps(
            {
                "event": "window_error",
                "level": "error",
                "message": "x" * (64 * 1024),
            }
        ).encode("utf-8")
        with self.assertRaises(HTTPError) as caught:
            _post(self.endpoint, body)
        self.assertEqual(caught.exception.code, 413)
        payload = json.loads(caught.exception.read().decode("utf-8"))
        caught.exception.close()
        self.assertEqual(payload["error"]["code"], "invalid_length")

    def test_requires_gateway_token_and_same_origin(self) -> None:
        body = json.dumps(
            {
                "event": "search_completed",
                "level": "info",
                "details": {"result_count": 15},
            }
        ).encode("utf-8")
        wrong_token_url = (
            self.server.address.origin + "/wrong-token/api/diagnostics/frontend"
        )
        with self.assertRaises(HTTPError) as wrong_token:
            _post(wrong_token_url, body)
        self.assertEqual(wrong_token.exception.code, 404)
        wrong_token.exception.close()

        with self.assertRaises(HTTPError) as wrong_origin:
            _post(self.endpoint, body, origin="https://example.invalid")
        self.assertEqual(wrong_origin.exception.code, 403)
        wrong_origin.exception.close()

    def test_write_failure_degrades_without_breaking_gateway(self) -> None:
        writer = self.server._diagnostic_log  # noqa: SLF001 - deliberate fault test
        self.assertIsNotNone(writer)
        assert writer is not None
        with patch.object(writer, "record", side_effect=OSError("disk unavailable")):
            status, response = _post(
                self.endpoint,
                json.dumps(
                    {
                        "event": "search_watchdog_timeout",
                        "level": "warning",
                        "message": "polling stopped",
                    }
                ).encode("utf-8"),
            )
        self.assertEqual(status, 202)
        self.assertTrue(response["accepted"])
        self.assertFalse(response["persisted"])
        self.assertEqual(response["code"], "diagnostic_write_failed")

        with urlopen(self.server.url + "api/bootstrap", timeout=5) as bootstrap:
            self.assertEqual(bootstrap.status, 200)


if __name__ == "__main__":
    unittest.main()
