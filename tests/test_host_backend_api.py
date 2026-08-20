from __future__ import annotations

import json
import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from zvec_host.backend_api import (
    BackendApiClient,
    BackendHttpError,
    BackendProtocolError,
    BackendTimeoutError,
    BackendTransportError,
)


class _CaptureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CaptureHandler)
        self.requests: list[dict[str, Any]] = []
        self.response_status = 200
        self.response_reason: str | None = None
        self.response_content_type = "application/json; charset=utf-8"
        self.response_body = b"{}"
        self.response_delay = 0.0


class _CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def capture_server(self) -> _CaptureServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:  # noqa: N802
        self._capture()

    def do_PUT(self) -> None:  # noqa: N802
        self._capture()

    def do_POST(self) -> None:  # noqa: N802
        self._capture()

    def do_DELETE(self) -> None:  # noqa: N802
        self._capture()

    def _capture(self) -> None:
        raw_length = self.headers.get("Content-Length")
        length = int(raw_length) if raw_length is not None else 0
        body = self.rfile.read(length) if length else b""
        self.capture_server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "content_length": raw_length,
                "body": body,
            }
        )
        if self.capture_server.response_delay:
            time.sleep(self.capture_server.response_delay)
        self.send_response(
            self.capture_server.response_status,
            self.capture_server.response_reason,
        )
        response_body = self.capture_server.response_body
        self.send_header("Content-Type", self.capture_server.response_content_type)
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        # Expected when a timeout test deliberately abandons the connection.
        with suppress(OSError):
            self.wfile.write(response_body)

    def log_message(self, _format: str, *args: object) -> None:
        pass


@contextmanager
def _running_server() -> Iterator[_CaptureServer]:
    server = _CaptureServer()
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _set_json_response(
    server: _CaptureServer,
    payload: object,
    *,
    status: int = 200,
    reason: str | None = None,
) -> None:
    server.response_status = status
    server.response_reason = reason
    server.response_content_type = "application/json; charset=utf-8"
    server.response_body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


class HostBackendApiClientTests(unittest.TestCase):
    def test_health_sends_bearer_token_and_accepts_starting_503(self) -> None:
        with _running_server() as server:
            _set_json_response(
                server,
                {"status": "starting", "service_ready": False},
                status=503,
            )
            client = BackendApiClient(
                f"http://127.0.0.1:{server.server_port}", "secret-token"
            )

            health = client.get_health()

            self.assertEqual(health["status"], "starting")
            self.assertEqual(len(server.requests), 1)
            request = server.requests[0]
            self.assertEqual(request["method"], "GET")
            self.assertEqual(request["path"], "/health")
            self.assertEqual(request["headers"]["Authorization"], "Bearer secret-token")
            self.assertEqual(request["headers"]["Accept"], "application/json")

    def test_json_requests_have_exact_content_length_and_utf8_body(self) -> None:
        with _running_server() as server:
            client = BackendApiClient(
                f"http://127.0.0.1:{server.server_port}/", "token"
            )

            _set_json_response(server, {"credentials_configured": True})
            configured = client.configure_credentials(
                "密钥-value", "https://example.invalid/模型"
            )
            self.assertTrue(configured["credentials_configured"])
            credential_request = server.requests[-1]
            credential_body = credential_request["body"]
            self.assertEqual(credential_request["method"], "PUT")
            self.assertEqual(credential_request["path"], "/v1/session/credentials")
            self.assertEqual(
                int(credential_request["content_length"]), len(credential_body)
            )
            self.assertEqual(
                credential_request["headers"]["Content-Type"],
                "application/json; charset=utf-8",
            )
            self.assertEqual(
                json.loads(credential_body),
                {
                    "dashscope_api_key": "密钥-value",
                    "api_url": "https://example.invalid/模型",
                },
            )

            _set_json_response(
                server,
                {"job": {"id": "job-1", "command": "search", "status": "queued"}},
                status=202,
            )
            job = client.submit_job("search", {"text": "原神"})
            self.assertEqual(job["id"], "job-1")
            submit_request = server.requests[-1]
            submit_body = submit_request["body"]
            self.assertEqual(submit_request["method"], "POST")
            self.assertEqual(submit_request["path"], "/v1/jobs")
            self.assertEqual(int(submit_request["content_length"]), len(submit_body))
            self.assertEqual(
                json.loads(submit_body),
                {"command": "search", "params": {"text": "原神"}},
            )

            _set_json_response(
                server,
                {
                    "accepted": True,
                    "status": "shutting_down",
                    "instance_id": "backend-1",
                },
                status=202,
            )
            shutdown = client.shutdown_if_idle()
            self.assertTrue(shutdown["accepted"])
            shutdown_request = server.requests[-1]
            shutdown_body = shutdown_request["body"]
            self.assertEqual(shutdown_request["method"], "POST")
            self.assertEqual(shutdown_request["path"], "/v1/control/shutdown")
            self.assertEqual(
                int(shutdown_request["content_length"]), len(shutdown_body)
            )
            self.assertEqual(json.loads(shutdown_body), {"if_idle": True})

    def test_image_edit_client_builds_multipart_and_uses_dedicated_routes(self) -> None:
        with _running_server() as server:
            client = BackendApiClient(f"http://127.0.0.1:{server.server_port}", "token")
            task = {
                "id": "a" * 32,
                "status": "queued",
                "source_name": "原图.png",
            }
            _set_json_response(server, {"task": task}, status=202)

            submitted = client.submit_image_edit_file(
                "原图.png",
                b"png-content",
                {
                    "model": "qwen-image-edit-plus",
                    "prompt": "修改背景",
                    "negative_prompt": "",
                    "prompt_extend": True,
                },
            )

            self.assertEqual(submitted["id"], "a" * 32)
            upload = server.requests[-1]
            self.assertEqual(upload["path"], "/v1/image-edit/tasks")
            self.assertTrue(
                upload["headers"]["Content-Type"].startswith("multipart/form-data;")
            )
            self.assertEqual(int(upload["content_length"]), len(upload["body"]))
            self.assertIn("修改背景".encode(), upload["body"])
            self.assertIn(b"png-content", upload["body"])
            self.assertGreater(
                upload["body"].rfind(b'name="file"'),
                upload["body"].find(b'name="metadata"'),
            )

            _set_json_response(
                server,
                {"tasks": [task], "count": 1, "total_count": 1},
            )
            listed = client.list_image_edit_tasks(active=True, limit=1)
            self.assertEqual(listed["tasks"], [task])
            self.assertEqual(
                server.requests[-1]["path"],
                "/v1/image-edit/tasks?active=true&limit=1",
            )

            _set_json_response(
                server,
                {"settings": {"configured": True, "output_directory": r"D:\Out"}},
            )
            settings = client.get_image_edit_settings()
            self.assertEqual(settings["output_directory"], r"D:\Out")
            self.assertEqual(server.requests[-1]["path"], "/v1/image-edit/settings")

            _set_json_response(
                server,
                {"abandoned_task_ids": ["a" * 32], "count": 1},
            )
            abandoned = client.abandon_image_edit_tasks()
            self.assertEqual(abandoned["count"], 1)
            self.assertEqual(json.loads(server.requests[-1]["body"]), {"confirm": True})

    def test_folder_name_tag_settings_use_dedicated_routes(self) -> None:
        with _running_server() as server:
            client = BackendApiClient(
                f"http://127.0.0.1:{server.server_port}", "token"
            )
            settings = {
                "blacklist": ["自拍", "V"],
                "revision": "a" * 64,
                "using_defaults": False,
            }
            _set_json_response(server, {"settings": settings})

            self.assertEqual(client.get_folder_name_tag_settings(), settings)
            self.assertEqual(
                server.requests[-1]["path"],
                "/v1/folder-name-tags/settings",
            )
            self.assertEqual(server.requests[-1]["method"], "GET")

            self.assertEqual(
                client.update_folder_name_tag_settings(["自拍", "V"]),
                settings,
            )
            self.assertEqual(server.requests[-1]["method"], "PUT")
            self.assertEqual(
                json.loads(server.requests[-1]["body"]),
                {"blacklist": ["自拍", "V"]},
            )

            with self.assertRaises(ValueError):
                client.update_folder_name_tag_settings("自拍")  # type: ignore[arg-type]

    def test_get_list_and_delete_job_routes(self) -> None:
        with _running_server() as server:
            client = BackendApiClient(f"http://127.0.0.1:{server.server_port}", "token")
            job = {"id": "abc 123", "command": "stats", "status": "running"}

            _set_json_response(server, {"job": job})
            self.assertEqual(client.get_job("abc 123"), job)
            self.assertEqual(server.requests[-1]["path"], "/v1/jobs/abc%20123")

            _set_json_response(server, {"count": 1, "jobs": [job]})
            jobs = client.list_jobs(active=True, limit=25)
            self.assertEqual(jobs["jobs"], [job])
            self.assertEqual(
                server.requests[-1]["path"], "/v1/jobs?active=true&limit=25"
            )

            _set_json_response(server, {"job": job}, status=202)
            self.assertEqual(client.cancel_job("abc 123"), job)
            self.assertEqual(server.requests[-1]["method"], "DELETE")
            self.assertEqual(server.requests[-1]["path"], "/v1/jobs/abc%20123")

    def test_http_error_exposes_backend_error_fields(self) -> None:
        with _running_server() as server:
            _set_json_response(
                server,
                {
                    "error": {
                        "code": "backend_busy",
                        "message": "A job is still active.",
                        "details": {"active_job_ids": ["job-1"]},
                    }
                },
                status=409,
                reason="Conflict",
            )
            client = BackendApiClient(f"http://127.0.0.1:{server.server_port}", "token")

            with self.assertRaises(BackendHttpError) as raised:
                client.get_job("job-1")

            error = raised.exception
            self.assertEqual(error.status_code, 409)
            self.assertEqual(error.status, 409)
            self.assertEqual(error.code, "backend_busy")
            self.assertEqual(error.backend_message, "A job is still active.")
            self.assertEqual(error.details, {"active_job_ids": ["job-1"]})
            assert error.payload is not None
            self.assertEqual(error.payload["error"]["code"], "backend_busy")

    def test_malformed_and_wrong_shape_json_raise_protocol_error(self) -> None:
        with _running_server() as server:
            client = BackendApiClient(f"http://127.0.0.1:{server.server_port}", "token")
            server.response_body = b"{not-json"

            with self.assertRaises(BackendProtocolError) as malformed:
                client.get_health()
            self.assertEqual(malformed.exception.status_code, 200)
            self.assertEqual(malformed.exception.response_body, "{not-json")

            server.response_body = b"[]"
            with self.assertRaises(BackendProtocolError) as wrong_shape:
                client.get_health()
            self.assertIn("must be an object", wrong_shape.exception.message)

            _set_json_response(server, {"not_job": {}})
            with self.assertRaises(BackendProtocolError) as missing_job:
                client.get_job("job-1")
            self.assertIn("job object", missing_job.exception.message)

    def test_wrong_content_type_is_a_protocol_error(self) -> None:
        with _running_server() as server:
            server.response_content_type = "text/plain"
            server.response_body = b"{}"
            client = BackendApiClient(f"http://127.0.0.1:{server.server_port}", "token")

            with self.assertRaises(BackendProtocolError) as raised:
                client.get_health()

            self.assertIn("application/json", raised.exception.message)

    def test_timeout_and_connection_failure_are_structured(self) -> None:
        with _running_server() as server:
            server.response_delay = 0.15
            client = BackendApiClient(
                f"http://127.0.0.1:{server.server_port}",
                "token",
                timeout=0.02,
            )

            with self.assertRaises(BackendTimeoutError) as raised:
                client.get_health()

            self.assertEqual(raised.exception.method, "GET")
            self.assertEqual(raised.exception.timeout_seconds, 0.02)

        closed_port = server.server_port
        client = BackendApiClient(f"http://127.0.0.1:{closed_port}", "token")
        with self.assertRaises(BackendTransportError) as transport:
            client.get_health()
        self.assertEqual(transport.exception.method, "GET")

    def test_input_validation_prevents_ambiguous_requests(self) -> None:
        with self.assertRaises(ValueError):
            BackendApiClient("not-a-url", "token")
        with self.assertRaises(ValueError):
            BackendApiClient("http://127.0.0.1:1", "")
        client = BackendApiClient("http://127.0.0.1:1", "token")
        with self.assertRaises(ValueError):
            client.list_jobs(active="true")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            client.list_jobs(limit=0)
        with self.assertRaises(ValueError):
            client.submit_job("", {})


if __name__ == "__main__":
    unittest.main()
