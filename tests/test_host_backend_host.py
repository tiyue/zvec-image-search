from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import patch

from zvec_host.backend_host import (
    BackendBusyError,
    BackendConfigurationError,
    BackendHost,
    BackendHostError,
    BackendProcessContainmentError,
    BackendProcessExited,
    BackendShutdownTimeout,
    BackendStartupTimeout,
)
from zvec_host.windows_job import WindowsJobError

_FAKE_BACKEND = r"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--mode", required=True)
parser.add_argument("command")
parser.add_argument("--host", required=True)
parser.add_argument("--port", required=True, type=int)
parser.add_argument("--query-root", required=True)
parser.add_argument("--libraries-config", required=True)
parser.add_argument("--instance-id", required=True)
parser.add_argument("--config-fingerprint", required=True)
parser.add_argument("--instance-lock-path", required=True)
args = parser.parse_args()

if args.command != "serve":
    raise SystemExit(64)
if args.mode == "early-exit":
    print("early-exit-marker", flush=True)
    raise SystemExit(7)
if args.mode == "no-listen":
    for index in range(20):
        print(f"startup-noise-{index}-" + ("x" * 1000), flush=True)
    time.sleep(60)
    raise SystemExit(70)

token = os.environ["ZVEC_BACKEND_TOKEN"]
manifest = json.loads(Path(args.libraries_config).read_text(encoding="utf-8"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)
        self.wfile.flush()

    def _authenticated(self):
        if self.headers.get("Authorization") == f"Bearer {token}":
            return True
        self._send(401, {"error": {"code": "unauthorized"}})
        return False

    def do_GET(self):
        if not self._authenticated():
            return
        if self.path != "/health":
            self._send(404, {"error": {"code": "not_found"}})
            return
        self._send(
            200,
            {
                "status": "ok",
                "service_ready": True,
                "worker_alive": True,
                "instance_id": args.instance_id,
                "config_fingerprint": args.config_fingerprint,
                "token_length": len(token),
                "config_home": os.environ.get("ZVEC_CONFIG_HOME"),
                "python_utf8": os.environ.get("PYTHONUTF8"),
                "python_io_encoding": os.environ.get("PYTHONIOENCODING"),
                "python_unbuffered": os.environ.get("PYTHONUNBUFFERED"),
                "manifest": manifest,
                "query_root": args.query_root,
                "lock_path": args.instance_lock_path,
            },
        )

    def do_POST(self):
        if not self._authenticated():
            return
        length = int(self.headers.get("Content-Length") or "0")
        payload = json.loads(self.rfile.read(length))
        if self.path != "/v1/control/shutdown" or payload != {"if_idle": True}:
            self._send(400, {"error": {"code": "invalid_request"}})
            return
        if args.mode == "busy":
            self._send(
                409,
                {
                    "error": {
                        "code": "backend_busy",
                        "message": "The backend still has active jobs.",
                        "details": {"active_job_ids": ["job-running"]},
                    }
                },
            )
            return
        self._send(
            202,
            {
                "accepted": True,
                "status": "shutting_down",
                "instance_id": args.instance_id,
            },
        )
        if args.mode != "ignore-shutdown":
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, _format, *args):
        pass


server = ThreadingHTTPServer((args.host, args.port), Handler)
print(
    json.dumps(
        {
            "event": "backend_listening",
            "host": server.server_address[0],
            "port": server.server_address[1],
            "instance_id": args.instance_id,
            "config_fingerprint": args.config_fingerprint,
        }
    ),
    flush=True,
)
try:
    server.serve_forever(poll_interval=0.01)
finally:
    server.server_close()
"""


class _RecordingJob:
    def __init__(self, *, fail_assignment: bool = False) -> None:
        self.fail_assignment = fail_assignment
        self.assigned_processes: list[Any] = []
        self.closed = False

    def assign(self, process: Any) -> None:
        self.assigned_processes.append(process)
        if self.fail_assignment:
            raise WindowsJobError(
                "AssignProcessToJobObject",
                winerror=5,
                detail="test assignment failure",
            )

    def close(self) -> None:
        self.closed = True


class HostBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Windows runners may expose TemporaryDirectory through an 8.3 alias.
        # Canonicalize once so fixtures and production-resolved paths agree.
        self.root = Path(self.temporary.name).resolve()
        self.config_home = self.root / "config-home"
        self.config_home.mkdir()
        self.image_root = self.root / "人物图库"
        self.image_root.mkdir()
        self.workspace = self.root / "workspace"
        self.results = self.root / "results"
        self.runtime_parent = self.root / "runtime-parent"
        self.config_path = self.config_home / "config.json"
        self.fake_backend = self.root / "fake_backend.py"
        self.fake_backend.write_text(_FAKE_BACKEND, encoding="utf-8")
        self._write_config()
        self.hosts: list[BackendHost] = []
        self.addCleanup(self._stop_hosts)

    def _write_config(self, *, schema_version: int = 3) -> None:
        payload = {
            "schema_version": schema_version,
            "results_directory": str(self.results),
            "default_library_id": "library-main",
            "libraries": [
                {
                    "id": "library-main",
                    "name": "人物写真",
                    "image_root": str(self.image_root),
                    "workspace_directory": str(self.workspace),
                    "enabled": True,
                },
                {
                    "id": "library-disabled",
                    "name": "停用图库",
                    "image_root": str(self.root / "missing-disabled-images"),
                    "workspace_directory": str(
                        self.root / "missing-disabled-workspace"
                    ),
                    "enabled": False,
                },
            ],
        }
        self.config_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _host(self, mode: str, **overrides: Any) -> BackendHost:
        options: dict[str, Any] = {
            "config_path": self.config_path,
            "config_home": self.config_home,
            "command_prefix": (
                sys.executable,
                str(self.fake_backend),
                "--mode",
                mode,
            ),
            "runtime_parent": self.runtime_parent,
            "startup_timeout": 2.0,
            "health_timeout": 0.2,
            "health_poll_interval": 0.01,
            "request_timeout": 0.5,
            "shutdown_timeout": 0.3,
            "max_output_lines": 8,
            "max_output_line_chars": 512,
        }
        options.update(overrides)
        host = BackendHost(**options)
        self.hosts.append(host)
        return host

    def _stop_hosts(self) -> None:
        for host in reversed(self.hosts):
            with suppress(Exception):  # pragma: no cover - best-effort cleanup
                host.stop(force=True)

    def test_default_command_uses_current_python_module(self) -> None:
        host = BackendHost(
            self.config_path,
            config_home=self.config_home,
            runtime_parent=self.runtime_parent,
        )
        self.assertEqual(host.command_prefix, (sys.executable, "-m", "image_service"))

    def test_frozen_webview_uses_sibling_backend_executable(self) -> None:
        application_directory = self.root / "frozen-app"
        application_directory.mkdir()
        host_name = (
            "Zvec.WebviewPreview.exe" if os.name == "nt" else "Zvec.WebviewPreview"
        )
        backend_name = "zvec-backend.exe" if os.name == "nt" else "zvec-backend"
        host_executable = application_directory / host_name
        backend_executable = application_directory / backend_name
        host_executable.write_bytes(b"webview")
        backend_executable.write_bytes(b"backend")

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(host_executable)),
        ):
            host = BackendHost(
                self.config_path,
                config_home=self.config_home,
                runtime_parent=self.runtime_parent,
            )

        self.assertEqual(host.command_prefix, (str(backend_executable.resolve()),))

    def test_frozen_webview_rejects_missing_sibling_backend(self) -> None:
        application_directory = self.root / "broken-frozen-app"
        application_directory.mkdir()
        host_name = (
            "Zvec.WebviewPreview.exe" if os.name == "nt" else "Zvec.WebviewPreview"
        )
        backend_name = "zvec-backend.exe" if os.name == "nt" else "zvec-backend"
        host_executable = application_directory / host_name
        host_executable.write_bytes(b"webview")

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(host_executable)),
            self.assertRaises(BackendHostError) as raised,
        ):
            BackendHost(
                self.config_path,
                config_home=self.config_home,
                runtime_parent=self.runtime_parent,
            )
        self.assertIn(backend_name, str(raised.exception))

    def test_start_generates_authenticated_runtime_manifest_and_stops_cleanly(
        self,
    ) -> None:
        host = self._host("ready")

        runtime = host.start()

        self.assertTrue(host.is_running)
        self.assertGreater(runtime.port, 0)
        self.assertEqual(runtime.host, "127.0.0.1")
        self.assertRegex(runtime.instance_id, r"^zvec-python-[0-9a-f]{32}$")
        self.assertRegex(runtime.config_fingerprint, r"^[0-9a-f]{64}$")
        token = host.session_token
        assert token is not None
        self.assertGreaterEqual(len(token), 64)
        command = host.launch_command
        assert command is not None
        self.assertEqual(command[command.index("--port") + 1], "0")
        self.assertNotIn(token, command)
        self.assertNotIn(token, "\n".join(host.output_lines))

        manifest = json.loads(runtime.libraries_manifest.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["default_library_id"], "library-main")
        self.assertEqual(len(manifest["libraries"]), 1)
        self.assertEqual(manifest["libraries"][0]["id"], "library-main")
        self.assertEqual(list(runtime.runtime_directory.glob("*.tmp")), [])

        health = host.client.get_health()
        self.assertEqual(health["token_length"], len(token))
        self.assertEqual(health["config_home"], str(self.config_home))
        self.assertEqual(health["python_utf8"], "1")
        self.assertEqual(health["python_io_encoding"], "utf-8")
        self.assertEqual(health["python_unbuffered"], "1")
        self.assertEqual(health["manifest"], manifest)
        self.assertEqual(health["query_root"], str(runtime.query_root))

        runtime_directory = runtime.runtime_directory
        host.stop()

        self.assertFalse(host.is_running)
        self.assertFalse(runtime_directory.exists())
        self.assertTrue(self.runtime_parent.is_dir())
        self.assertTrue(self.config_home.is_dir())
        self.assertTrue(self.image_root.is_dir())
        self.assertTrue(self.workspace.is_dir())
        self.assertTrue(self.results.is_dir())

    def test_process_job_is_held_until_backend_exits_cleanly(self) -> None:
        host = self._host("ready")
        process_job = _RecordingJob()

        with patch.object(
            host,
            "_create_windows_job",
            return_value=process_job,
        ):
            host.start()

        self.assertEqual(len(process_job.assigned_processes), 1)
        self.assertIsNone(process_job.assigned_processes[0].poll())
        self.assertFalse(process_job.closed)

        host.stop()

        self.assertTrue(process_job.closed)
        self.assertIsNotNone(process_job.assigned_processes[0].poll())

    def test_job_creation_failure_starts_no_child_and_cleans_runtime(self) -> None:
        host = self._host("ready")
        failure = WindowsJobError(
            "CreateJobObjectW",
            winerror=5,
            detail="test creation failure",
        )

        with (
            patch.object(host, "_create_windows_job", side_effect=failure),
            patch("zvec_host.backend_host.subprocess.Popen") as popen,
            self.assertRaises(BackendProcessContainmentError) as raised,
        ):
            host.start()

        self.assertEqual(raised.exception.code, "backend_process_containment_failed")
        self.assertEqual(raised.exception.stage, "job creation")
        self.assertIsNone(raised.exception.child_terminated)
        popen.assert_not_called()
        self.assertFalse(host.is_running)
        self.assertEqual(list(self.runtime_parent.iterdir()), [])

    def test_job_assignment_failure_terminates_started_child(self) -> None:
        host = self._host("no-listen")
        process_job = _RecordingJob(fail_assignment=True)

        with (
            patch.object(
                host,
                "_create_windows_job",
                return_value=process_job,
            ),
            self.assertRaises(BackendProcessContainmentError) as raised,
        ):
            host.start()

        self.assertEqual(raised.exception.stage, "process assignment")
        self.assertTrue(raised.exception.child_terminated)
        self.assertTrue(process_job.closed)
        self.assertEqual(len(process_job.assigned_processes), 1)
        self.assertIsNotNone(process_job.assigned_processes[0].poll())
        self.assertFalse(host.is_running)
        self.assertEqual(list(self.runtime_parent.iterdir()), [])

    def test_busy_backend_is_preserved_until_force_is_explicit(self) -> None:
        host = self._host("busy")
        runtime = host.start()

        with self.assertRaises(BackendBusyError) as raised:
            host.stop()

        self.assertEqual(raised.exception.active_job_ids, ("job-running",))
        self.assertTrue(host.is_running)
        self.assertTrue(runtime.runtime_directory.is_dir())

        host.stop(force=True)

        self.assertFalse(host.is_running)
        self.assertFalse(runtime.runtime_directory.exists())
        self.assertTrue(self.workspace.is_dir())

    def test_shutdown_timeout_preserves_process_without_force(self) -> None:
        host = self._host("ignore-shutdown", shutdown_timeout=0.1)
        runtime = host.start()

        with self.assertRaises(BackendShutdownTimeout):
            host.stop()

        self.assertTrue(host.is_running)
        self.assertTrue(runtime.runtime_directory.is_dir())

        host.stop(force=True)
        self.assertFalse(runtime.runtime_directory.exists())

    def test_early_exit_reports_tail_and_cleans_only_owned_runtime(self) -> None:
        host = self._host("early-exit")

        with self.assertRaises(BackendProcessExited) as raised:
            host.start()

        self.assertEqual(raised.exception.returncode, 7)
        self.assertIn("early-exit-marker", raised.exception.output_tail)
        self.assertFalse(host.is_running)
        self.assertEqual(list(self.runtime_parent.iterdir()), [])
        self.assertTrue(self.config_home.is_dir())
        self.assertTrue(self.image_root.is_dir())
        self.assertTrue(self.workspace.is_dir())
        self.assertTrue(self.results.is_dir())

    def test_startup_timeout_bounds_output_and_terminates_failed_launch(self) -> None:
        host = self._host(
            "no-listen",
            startup_timeout=0.2,
            max_output_lines=5,
            max_output_line_chars=256,
        )

        with self.assertRaises(BackendStartupTimeout):
            host.start()

        self.assertFalse(host.is_running)
        self.assertLessEqual(len(host.output_lines), 5)
        self.assertTrue(all(len(line) <= 257 for line in host.output_lines))
        self.assertEqual(list(self.runtime_parent.iterdir()), [])

    def test_non_v3_configuration_is_rejected_before_process_launch(self) -> None:
        self._write_config(schema_version=2)
        host = self._host("ready")

        with self.assertRaises(BackendConfigurationError):
            host.start()

        self.assertIsNone(host.launch_command)
        self.assertFalse(self.runtime_parent.exists())
        self.assertTrue(self.image_root.is_dir())


if __name__ == "__main__":
    unittest.main()
