"""Lifecycle host for the persistent native backend.

The local process owns only a generated runtime manifest and query-staging
directory. User image roots, Collection workspaces, results, and ConfigHome are
never treated as temporary data and are therefore never removed by this host.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zvec_launcher import (
    CONFIG_SCHEMA_VERSION,
    LauncherError,
    NativeConfig,
    get_config_home,
    validate_native_config,
)

from .backend_api import BackendApiClient, BackendApiError, BackendHttpError
from .windows_job import (
    WindowsJobError,
    WindowsKillOnCloseJob,
    create_kill_on_close_job,
)

_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_DEFAULT_STARTUP_TIMEOUT = 45.0
_DEFAULT_HEALTH_TIMEOUT = 1.0
_DEFAULT_HEALTH_POLL_INTERVAL = 0.1
_DEFAULT_REQUEST_TIMEOUT = 30.0
_DEFAULT_SHUTDOWN_TIMEOUT = 5.0
_DEFAULT_MAX_OUTPUT_LINES = 200
_DEFAULT_MAX_OUTPUT_LINE_CHARS = 4096


class BackendHostError(RuntimeError):
    """Base class for backend process lifecycle failures."""


class BackendConfigurationError(BackendHostError):
    """The schema-v3 desktop configuration cannot safely start a backend."""


class BackendStartupError(BackendHostError):
    """The owned backend did not reach the ready state."""


class BackendProcessContainmentError(BackendStartupError):
    """The backend could not be safely contained in its Windows Job Object."""

    code = "backend_process_containment_failed"

    def __init__(
        self,
        stage: str,
        detail: str,
        *,
        child_terminated: bool | None,
    ) -> None:
        self.stage = stage
        self.detail = detail
        self.child_terminated = child_terminated
        cleanup = (
            ""
            if child_terminated is None
            else (
                " The started backend was terminated."
                if child_terminated
                else " The started backend could not be confirmed terminated."
            )
        )
        super().__init__(
            f"Windows backend process containment failed during {stage}: "
            f"{detail}.{cleanup}"
        )


class BackendProcessExited(BackendStartupError):
    """The backend process exited before it became ready."""

    def __init__(self, returncode: int, output_tail: str) -> None:
        self.returncode = returncode
        self.output_tail = output_tail
        message = f"Backend exited before it became ready (exit code {returncode})."
        if output_tail:
            message = f"{message}\nBackend output:\n{output_tail}"
        super().__init__(message)


class BackendStartupTimeout(BackendStartupError):
    """The backend did not listen and report ready within the startup deadline."""

    def __init__(self, timeout_seconds: float, detail: str, output_tail: str) -> None:
        self.timeout_seconds = timeout_seconds
        self.detail = detail
        self.output_tail = output_tail
        message = (
            f"Backend did not become ready within {timeout_seconds:g} seconds: {detail}"
        )
        if output_tail:
            message = f"{message}\nBackend output:\n{output_tail}"
        super().__init__(message)


class BackendBusyError(BackendHostError):
    """Safe shutdown was refused because jobs are still active."""

    def __init__(self, active_job_ids: Sequence[str] = ()) -> None:
        self.active_job_ids = tuple(active_job_ids)
        suffix = (
            f" Active jobs: {', '.join(self.active_job_ids)}."
            if self.active_job_ids
            else ""
        )
        super().__init__(
            "Backend still has active work; it was left running and was not "
            f"terminated.{suffix}"
        )


class BackendShutdownTimeout(BackendHostError):
    """The backend accepted shutdown but did not exit before the deadline."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(
            "Backend accepted safe shutdown but did not exit within "
            f"{timeout_seconds:g} seconds; it was not forcibly terminated."
        )


@dataclass(frozen=True, slots=True)
class BackendRuntime:
    """Verified connection details for one ready backend process."""

    base_url: str
    host: str
    port: int
    pid: int
    instance_id: str
    config_fingerprint: str
    runtime_directory: Path
    libraries_manifest: Path
    query_root: Path


class BackendHost:
    """Start and safely stop the isolated persistent Python backend.

    ``command_prefix`` exists for process-level contract tests. Production code
    should leave it unset. Source/wheel runs use this interpreter, while a frozen
    WebView host uses the sibling ``zvec-backend`` executable that shares its
    bundled runtime without routing backend work back through the GUI executable.
    """

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        config_home: str | Path | None = None,
        command_prefix: Sequence[str] | None = None,
        runtime_parent: str | Path | None = None,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT,
        health_timeout: float = _DEFAULT_HEALTH_TIMEOUT,
        health_poll_interval: float = _DEFAULT_HEALTH_POLL_INTERVAL,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
        max_output_lines: int = _DEFAULT_MAX_OUTPUT_LINES,
        max_output_line_chars: int = _DEFAULT_MAX_OUTPUT_LINE_CHARS,
    ) -> None:
        supplied_path = Path(config_path).expanduser() if config_path else None
        if config_home is not None:
            resolved_config_home = Path(config_home).expanduser().resolve()
        elif supplied_path is not None:
            resolved_config_home = supplied_path.absolute().parent.resolve()
        else:
            resolved_config_home = get_config_home()
        self._config_home = resolved_config_home
        self._config_path = (
            supplied_path.absolute()
            if supplied_path is not None
            else resolved_config_home / "config.json"
        )

        prefix = tuple(
            command_prefix
            if command_prefix is not None
            else _default_backend_command_prefix()
        )
        if not prefix or any(not isinstance(item, str) or not item for item in prefix):
            raise ValueError("command_prefix must contain non-empty strings")
        self._command_prefix = prefix
        self._runtime_parent = (
            Path(runtime_parent).expanduser().resolve()
            if runtime_parent is not None
            else Path(tempfile.gettempdir()).resolve()
            / "zvec-image-search"
            / "backend-host"
        )
        self._startup_timeout = _positive_finite(startup_timeout, "startup_timeout")
        self._health_timeout = _positive_finite(health_timeout, "health_timeout")
        self._health_poll_interval = _positive_finite(
            health_poll_interval, "health_poll_interval"
        )
        self._request_timeout = _positive_finite(request_timeout, "request_timeout")
        self._shutdown_timeout = _positive_finite(shutdown_timeout, "shutdown_timeout")
        if (
            isinstance(max_output_lines, bool)
            or not isinstance(max_output_lines, int)
            or max_output_lines < 1
        ):
            raise ValueError("max_output_lines must be a positive integer")
        if (
            isinstance(max_output_line_chars, bool)
            or not isinstance(max_output_line_chars, int)
            or max_output_line_chars < 128
        ):
            raise ValueError("max_output_line_chars must be at least 128")
        self._max_output_line_chars = max_output_line_chars

        self._lifecycle_lock = threading.RLock()
        self._output_lock = threading.Lock()
        self._output: deque[str] = deque(maxlen=max_output_lines)
        self._listening_event = threading.Event()
        self._listening_payload: dict[str, Any] | None = None
        self._listening_error: str | None = None

        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._client: BackendApiClient | None = None
        self._runtime: BackendRuntime | None = None
        self._owned_runtime_directory: Path | None = None
        self._windows_job: WindowsKillOnCloseJob | None = None
        self._session_token: str | None = None
        self._instance_id: str | None = None
        self._config_fingerprint: str | None = None
        self._last_launch_command: tuple[str, ...] | None = None

    @property
    def config_home(self) -> Path:
        return self._config_home

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def command_prefix(self) -> tuple[str, ...]:
        return self._command_prefix

    @property
    def launch_command(self) -> tuple[str, ...] | None:
        return self._last_launch_command

    @property
    def runtime(self) -> BackendRuntime | None:
        return self._runtime

    @property
    def client(self) -> BackendApiClient:
        client = self._client
        if client is None or not self.is_running:
            raise BackendHostError("Backend has not been started or has exited.")
        return client

    @property
    def session_token(self) -> str | None:
        """Return the in-memory token needed by trusted host components."""

        return self._session_token

    @property
    def is_running(self) -> bool:
        process = self._process
        return (
            process is not None and process.poll() is None and self._runtime is not None
        )

    @property
    def output_lines(self) -> tuple[str, ...]:
        """Return a bounded, token-redacted snapshot of backend output."""

        with self._output_lock:
            return tuple(self._output)

    def start(self) -> BackendRuntime:
        """Start the backend and wait until its authenticated health is ready."""

        with self._lifecycle_lock:
            if self.is_running:
                assert self._runtime is not None
                return self._runtime
            if self._process is not None:
                self._complete_stop()

            deadline = time.monotonic() + self._startup_timeout
            try:
                config = self._load_config()
                self._prepare_user_directories(config)
                self._instance_id = f"zvec-python-{uuid.uuid4().hex}"
                self._session_token = secrets.token_urlsafe(48)
                runtime_directory = self._create_runtime_directory()
                query_root = runtime_directory / "query-staging"
                query_root.mkdir(mode=0o700)
                manifest_path = runtime_directory / "libraries.json"
                manifest = _runtime_manifest(config)
                self._config_fingerprint = _configuration_fingerprint(
                    manifest, self._command_prefix
                )
                _atomic_write_json(manifest_path, manifest)

                command = self._build_command(manifest_path, query_root)
                self._last_launch_command = tuple(command)
                self._reset_output_state()
                process = self._spawn(command, manifest_path)
                self._process = process
                self._start_output_reader(process)
                listening = self._wait_for_listening(deadline)
                port = _listening_port(listening)
                base_url = f"http://127.0.0.1:{port}/"
                self._wait_until_ready(base_url, deadline)

                runtime = BackendRuntime(
                    base_url=base_url,
                    host="127.0.0.1",
                    port=port,
                    pid=process.pid,
                    instance_id=self._required_instance_id(),
                    config_fingerprint=self._required_fingerprint(),
                    runtime_directory=runtime_directory,
                    libraries_manifest=manifest_path,
                    query_root=query_root,
                )
                self._runtime = runtime
                self._client = BackendApiClient(
                    base_url,
                    self._required_token(),
                    timeout=self._request_timeout,
                )
                return runtime
            except BaseException:
                # A process that never reached ready cannot own accepted jobs. It is
                # safe to terminate only this failed launch before removing our temp
                # files; user-configured directories are never part of that cleanup.
                self._abort_failed_start()
                raise

    def stop(self, *, force: bool = False) -> None:
        """Stop the backend without interrupting jobs unless ``force`` is true."""

        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        with self._lifecycle_lock:
            process = self._process
            if process is None:
                self._release_windows_job()
                self._cleanup_owned_runtime_directory()
                self._reset_runtime_state()
                return
            if process.poll() is not None:
                self._complete_stop()
                return

            graceful_accepted = False
            client = self._client
            if client is not None:
                try:
                    client.shutdown_if_idle()
                    graceful_accepted = True
                except BackendHttpError as exc:
                    if exc.status_code == 409 and exc.code == "backend_busy":
                        if not force:
                            raise BackendBusyError(
                                _active_job_ids(exc.details)
                            ) from exc
                    elif not force:
                        raise BackendHostError(
                            "Backend refused the safe shutdown request and was left "
                            "running."
                        ) from exc
                except BackendApiError as exc:
                    if process.poll() is not None:
                        self._complete_stop()
                        return
                    if not force:
                        raise BackendHostError(
                            "Could not confirm that the backend is idle; it was left "
                            "running."
                        ) from exc
            elif not force:
                raise BackendHostError(
                    "Backend readiness is unknown; use force=True only when "
                    "terminating this owned process is intentional."
                )

            if graceful_accepted and self._wait_for_exit(self._shutdown_timeout):
                self._complete_stop()
                return
            if graceful_accepted and not force:
                raise BackendShutdownTimeout(self._shutdown_timeout)

            # Reaching this branch requires explicit force. We still attempted the
            # idle-only protocol first so normal shutdown never races active work.
            self._terminate_owned_process()
            self._complete_stop()

    close = stop

    def _load_config(self) -> NativeConfig:
        path = self._config_path
        if path.is_symlink():
            raise BackendConfigurationError(
                f"Desktop configuration must not be a symbolic link: {path}"
            )
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise BackendConfigurationError(
                f"Could not read desktop configuration: {path}"
            ) from exc
        if size > _MAX_CONFIG_BYTES:
            raise BackendConfigurationError(
                f"Desktop configuration exceeds {_MAX_CONFIG_BYTES} bytes: {path}"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackendConfigurationError(
                f"Desktop configuration is not valid UTF-8 JSON: {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise BackendConfigurationError(
                "Desktop configuration must contain a JSON object."
            )
        try:
            return validate_native_config(payload)
        except LauncherError as exc:
            raise BackendConfigurationError(str(exc)) from exc

    def _prepare_user_directories(self, config: NativeConfig) -> None:
        try:
            config.results_directory.mkdir(parents=True, exist_ok=True)
            (self._config_home / "backend").mkdir(parents=True, exist_ok=True)
            for library in config.libraries:
                if not library.enabled:
                    continue
                if not library.image_root.is_dir():
                    raise BackendConfigurationError(
                        f"Enabled library image root is missing: {library.image_root}"
                    )
                library.workspace_directory.mkdir(parents=True, exist_ok=True)
        except BackendConfigurationError:
            raise
        except OSError as exc:
            raise BackendConfigurationError(
                f"Could not prepare a configured runtime directory: {exc}"
            ) from exc

    def _create_runtime_directory(self) -> Path:
        try:
            self._runtime_parent.mkdir(parents=True, exist_ok=True)
            created = Path(
                tempfile.mkdtemp(
                    prefix=f"{self._required_instance_id()}-",
                    dir=self._runtime_parent,
                )
            ).resolve()
        except OSError as exc:
            raise BackendHostError(
                "Could not create backend runtime directory."
            ) from exc
        self._owned_runtime_directory = created
        return created

    def _build_command(self, manifest_path: Path, query_root: Path) -> list[str]:
        lock_path = self._config_home / "backend" / "backend.lock"
        return [
            *self._command_prefix,
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--query-root",
            str(query_root),
            "--libraries-config",
            str(manifest_path),
            "--instance-id",
            self._required_instance_id(),
            "--config-fingerprint",
            self._required_fingerprint(),
            "--instance-lock-path",
            str(lock_path),
        ]

    def _spawn(
        self, command: Sequence[str], manifest_path: Path
    ) -> subprocess.Popen[str]:
        try:
            process_job = self._create_windows_job()
        except WindowsJobError as exc:
            raise BackendProcessContainmentError(
                "job creation",
                str(exc),
                child_terminated=None,
            ) from exc
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "ZVEC_CONFIG_HOME": str(self._config_home),
                "ZVEC_BACKEND_TOKEN": self._required_token(),
                "ZVEC_BACKEND_INSTANCE_ID": self._required_instance_id(),
                "ZVEC_BACKEND_CONFIG_FINGERPRINT": self._required_fingerprint(),
                "ZVEC_BACKEND_LOCK_PATH": str(
                    self._config_home / "backend" / "backend.lock"
                ),
                "ZVEC_LIBRARIES_CONFIG": str(manifest_path),
                "ZVEC_MODELS_CONFIG": str(self._config_home / "models.json"),
            }
        )
        environment.pop("ZVEC_DOCKER_CONFIG_HOME", None)
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                shell=False,
                creationflags=creationflags,
            )
        except OSError as exc:
            if process_job is not None:
                with suppress(WindowsJobError):
                    process_job.close()
            raise BackendStartupError(
                f"Could not start Python backend with {command[0]!r}: {exc}"
            ) from exc
        if process_job is None:
            return process
        try:
            process_job.assign(process)
        except WindowsJobError as exc:
            with suppress(WindowsJobError):
                process_job.close()
            child_terminated = self._terminate_spawned_process(process)
            if not child_terminated:
                # Preserve ownership so the outer failed-start cleanup retries
                # termination instead of discarding the only process handle.
                self._process = process
            raise BackendProcessContainmentError(
                "process assignment",
                str(exc),
                child_terminated=child_terminated,
            ) from exc
        self._windows_job = process_job
        return process

    @staticmethod
    def _create_windows_job() -> WindowsKillOnCloseJob | None:
        if os.name != "nt":
            return None
        return create_kill_on_close_job()

    def _terminate_spawned_process(self, process: subprocess.Popen[str]) -> bool:
        """Best-effort cleanup before a failed spawn is published to the host."""

        if process.poll() is None:
            with suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=self._shutdown_timeout)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    process.kill()
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=self._shutdown_timeout)
        terminated = process.poll() is not None
        if terminated and process.stdout is not None:
            with suppress(OSError):
                process.stdout.close()
        return terminated

    def _start_output_reader(self, process: subprocess.Popen[str]) -> None:
        reader = threading.Thread(
            target=self._consume_output,
            args=(process,),
            name=f"zvec-backend-output-{process.pid}",
            daemon=True,
        )
        self._reader_thread = reader
        reader.start()

    def _consume_output(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            while True:
                # Supplying a size also bounds memory when a broken backend emits
                # one line without a newline. The deque bounds aggregate retention.
                chunk = stream.readline(self._max_output_line_chars + 1)
                if chunk == "":
                    break
                line = chunk.rstrip("\r\n")
                self._record_output(line)
                self._inspect_listening_event(line)
        except (OSError, ValueError) as exc:
            self._record_output(f"[output reader failed: {type(exc).__name__}]")

    def _record_output(self, line: str) -> None:
        token = self._session_token
        safe_line = line.replace(token, "<redacted>") if token else line
        with self._output_lock:
            self._output.append(safe_line)

    def _inspect_listening_event(self, line: str) -> None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("event") != "backend_listening":
            return
        error: str | None = None
        try:
            _listening_port(payload)
            host = payload.get("host")
            if host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("backend reported a non-loopback listening address")
            if payload.get("instance_id") != self._required_instance_id():
                raise ValueError("backend listening instance_id does not match")
            if payload.get("config_fingerprint") != self._required_fingerprint():
                raise ValueError("backend listening config_fingerprint does not match")
        except (BackendHostError, ValueError) as exc:
            error = str(exc)
        with self._output_lock:
            if self._listening_payload is None and self._listening_error is None:
                self._listening_payload = payload if error is None else None
                self._listening_error = error
                self._listening_event.set()

    def _wait_for_listening(self, deadline: float) -> dict[str, Any]:
        while True:
            process = self._required_process()
            returncode = process.poll()
            if returncode is not None:
                self._join_reader()
                raise BackendProcessExited(returncode, self._output_tail())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendStartupTimeout(
                    self._startup_timeout,
                    "no valid backend_listening event was received",
                    self._output_tail(),
                )
            if not self._listening_event.wait(min(0.05, remaining)):
                continue
            with self._output_lock:
                error = self._listening_error
                payload = self._listening_payload
            if error is not None:
                raise BackendStartupError(f"Invalid backend_listening event: {error}")
            if payload is not None:
                return payload

    def _wait_until_ready(self, base_url: str, deadline: float) -> None:
        last_detail = "health endpoint has not responded"
        while True:
            process = self._required_process()
            returncode = process.poll()
            if returncode is not None:
                self._join_reader()
                raise BackendProcessExited(returncode, self._output_tail())
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BackendStartupTimeout(
                    self._startup_timeout, last_detail, self._output_tail()
                )
            health_client = BackendApiClient(
                base_url,
                self._required_token(),
                timeout=min(self._health_timeout, remaining),
            )
            try:
                health = health_client.get_health()
                self._validate_health_identity(health)
                if (
                    health.get("status") == "ok"
                    and health.get("service_ready") is True
                    and health.get("worker_alive") is True
                ):
                    return
                error = health.get("error")
                if isinstance(error, Mapping) and error.get("code") == (
                    "library_initialization_failed"
                ):
                    raise BackendStartupError(
                        "Backend library initialization failed: "
                        f"{error.get('message') or 'unknown error'}"
                    )
                last_detail = _health_description(health)
            except BackendStartupError:
                raise
            except BackendApiError as exc:
                last_detail = str(exc)
            delay = min(
                self._health_poll_interval,
                max(0.0, deadline - time.monotonic()),
            )
            if delay:
                time.sleep(delay)

    def _validate_health_identity(self, health: Mapping[str, Any]) -> None:
        if health.get("instance_id") != self._required_instance_id():
            raise BackendStartupError(
                "Backend health instance_id does not match the owned process."
            )
        if health.get("config_fingerprint") != self._required_fingerprint():
            raise BackendStartupError(
                "Backend health config_fingerprint does not match the runtime config."
            )

    def _wait_for_exit(self, timeout: float) -> bool:
        process = self._required_process()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def _terminate_owned_process(self) -> None:
        process = self._required_process()
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=self._shutdown_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=self._shutdown_timeout)
            except subprocess.TimeoutExpired as exc:
                raise BackendHostError(
                    "Owned backend could not be terminated; runtime files were "
                    "retained."
                ) from exc
        except OSError as exc:
            if process.poll() is None:
                raise BackendHostError(
                    "Owned backend could not be terminated; runtime files were "
                    "retained."
                ) from exc

    def _abort_failed_start(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            try:
                self._terminate_owned_process()
            except BackendHostError:
                # Do not hide the authoritative startup error. If termination
                # failed, preserve the owned directory rather than deleting files
                # that a still-running process may use.
                return
        self._complete_stop()

    def _complete_stop(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            raise BackendHostError(
                "Refusing to clean runtime files while the owned backend is alive."
            )
        self._join_reader()
        if process is not None and process.stdout is not None:
            with suppress(OSError):
                process.stdout.close()
        self._release_windows_job()
        self._cleanup_owned_runtime_directory()
        self._reset_runtime_state()

    def _release_windows_job(self) -> None:
        job = self._windows_job
        if job is None:
            return
        try:
            job.close()
        except WindowsJobError as exc:
            raise BackendHostError(
                "Could not close the backend Windows Job Object handle."
            ) from exc
        self._windows_job = None

    def _join_reader(self) -> None:
        reader = self._reader_thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)

    def _cleanup_owned_runtime_directory(self) -> None:
        path = self._owned_runtime_directory
        if path is None:
            return
        # Only the exact directory returned by mkdtemp is eligible. ConfigHome,
        # image roots, workspaces, results, and runtime_parent are never removed.
        try:
            path.relative_to(self._runtime_parent)
        except ValueError as exc:
            raise BackendHostError(
                f"Refusing to remove runtime directory outside its owner root: {path}"
            ) from exc
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise BackendHostError(
                f"Could not remove owned backend runtime directory: {path}"
            ) from exc
        self._owned_runtime_directory = None

    def _reset_runtime_state(self) -> None:
        self._process = None
        self._reader_thread = None
        self._client = None
        self._runtime = None
        self._session_token = None
        self._instance_id = None
        self._config_fingerprint = None

    def _reset_output_state(self) -> None:
        with self._output_lock:
            self._output.clear()
            self._listening_payload = None
            self._listening_error = None
            self._listening_event.clear()

    def _output_tail(self) -> str:
        return "\n".join(self.output_lines)

    def _required_process(self) -> subprocess.Popen[str]:
        if self._process is None:
            raise BackendHostError("Backend process has not been created.")
        return self._process

    def _required_token(self) -> str:
        if self._session_token is None:
            raise BackendHostError("Backend session token has not been generated.")
        return self._session_token

    def _required_instance_id(self) -> str:
        if self._instance_id is None:
            raise BackendHostError("Backend instance ID has not been generated.")
        return self._instance_id

    def _required_fingerprint(self) -> str:
        if self._config_fingerprint is None:
            raise BackendHostError("Backend config fingerprint has not been generated.")
        return self._config_fingerprint


def _default_backend_command_prefix() -> tuple[str, ...]:
    if not bool(getattr(sys, "frozen", False)):
        return (sys.executable, "-m", "image_service")

    executable_name = "zvec-backend.exe" if os.name == "nt" else "zvec-backend"
    try:
        application_directory = Path(sys.executable).resolve(strict=True).parent
        backend_executable = (application_directory / executable_name).resolve(
            strict=True
        )
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise BackendHostError(
            f"Frozen backend executable is missing: {executable_name}"
        ) from exc
    if (
        not backend_executable.is_file()
        or backend_executable.parent != application_directory
    ):
        raise BackendHostError(
            f"Frozen backend executable is invalid: {backend_executable}"
        )
    return (str(backend_executable),)


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    normalized = float(value)
    if normalized <= 0 or not math.isfinite(normalized):
        raise ValueError(f"{name} must be a positive finite number")
    return normalized


def _runtime_manifest(config: NativeConfig) -> dict[str, Any]:
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "default_library_id": config.default_library_id,
        "results_directory": str(config.results_directory),
        "libraries": [
            library.to_dict() for library in config.libraries if library.enabled
        ],
    }


def _configuration_fingerprint(
    manifest: Mapping[str, Any], command_prefix: Sequence[str]
) -> str:
    identity = {
        "schema": "zvec-python-backend-host-v1",
        "runtime_manifest": manifest,
        "command_prefix": list(command_prefix),
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise BackendHostError(f"Could not write runtime manifest: {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _listening_port(payload: Mapping[str, Any]) -> int:
    port = payload.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("backend_listening port must be between 1 and 65535")
    return port


def _health_description(health: Mapping[str, Any]) -> str:
    return (
        f"status={health.get('status')!r}, "
        f"service_ready={health.get('service_ready')!r}, "
        f"worker_alive={health.get('worker_alive')!r}"
    )


def _active_job_ids(details: Any) -> tuple[str, ...]:
    if not isinstance(details, Mapping):
        return ()
    values = details.get("active_job_ids")
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, str) and value)


__all__ = [
    "BackendBusyError",
    "BackendConfigurationError",
    "BackendHost",
    "BackendHostError",
    "BackendProcessContainmentError",
    "BackendProcessExited",
    "BackendRuntime",
    "BackendShutdownTimeout",
    "BackendStartupError",
    "BackendStartupTimeout",
]
