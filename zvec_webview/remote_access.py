"""Own the bundled FRPC child process that exposes the LAN gateway publicly."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, Final

from .remote_settings import (
    RemoteFrpConfig,
    RemoteSettingsError,
    RemoteSettingsStore,
)

_CREATE_NO_WINDOW: Final = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_RETRY_DELAYS: Final = (1.0, 2.0, 5.0, 10.0, 30.0)
_TERMINATE_TIMEOUT: Final = 5.0
_JOIN_TIMEOUT: Final = 10.0

_LOGIN_SUCCESS = "login to server success"
_LOGIN_FAILED = "login to server failed"
_START_PROXY_SUCCESS = "start proxy success"
_START_PROXY_ERROR = "start proxy error"
_RECONNECT_HINTS: Final = (
    "try to reconnect",
    "reconnect to server",
    "connection to server closed",
    "connect to server error:",
    "heartbeat timeout",
)


class RemoteAccessError(RuntimeError):
    """A safe remote-access error suitable for the loopback JSON gateway."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class RemoteAccessController:
    """Manage the single owned FRPC child and its public status contract."""

    def __init__(
        self,
        config_home: str | Path,
        *,
        lan_endpoint: Callable[[], tuple[str, int]],
        store: RemoteSettingsStore | None = None,
        executable: Callable[[], Path] | None = None,
        popen_factory: Callable[..., Any] | None = None,
        retry_delays: Sequence[float] = _RETRY_DELAYS,
    ) -> None:
        self._store = store or RemoteSettingsStore(config_home)
        self._lan_endpoint = lan_endpoint
        self._executable = executable or _bundled_frpc_executable
        self._popen = popen_factory or subprocess.Popen
        self._retry_delays = tuple(float(delay) for delay in retry_delays)
        if not self._retry_delays:
            raise ValueError("retry_delays must not be empty")
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._process: Any | None = None
        self._worker: threading.Thread | None = None
        self._retry_index = 0
        self._state = "disabled"
        self._message = "远程访问未开启。"

    # -- public API ---------------------------------------------------------

    @property
    def public_url(self) -> str:
        """Return the configured public URL even while remote access is off."""

        try:
            config = self._store.load_config()
        except RemoteSettingsError:
            return ""
        return config.public_origin() if config is not None else ""

    def preference_enabled(self) -> bool:
        try:
            return self._store.load_preferences().enabled
        except RemoteSettingsError:
            return False

    def is_active(self) -> bool:
        with self._lock:
            worker = self._worker
        return worker is not None and worker.is_alive()

    def status(self) -> dict[str, Any]:
        try:
            config = self._store.load_config()
        except RemoteSettingsError:
            config = None
        try:
            preferences = self._store.load_preferences()
        except RemoteSettingsError:
            preferences = None
        enabled = bool(preferences.enabled) if preferences is not None else False
        with self._lock:
            state = self._state
            message = self._message
        if config is None:
            return {
                "configured": False,
                "enabled": enabled,
                "state": "unconfigured",
                "message": "尚未导入 FRPC 配置。",
                "public_url": "",
                "server_host": "",
                "server_port": None,
                "remote_port": None,
            }
        return {
            "configured": True,
            "enabled": enabled,
            "state": state,
            "message": message,
            "public_url": config.public_origin(),
            "server_host": config.server_addr,
            "server_port": config.server_port,
            "remote_port": config.remote_port,
        }

    def import_config(self, source_path: Any) -> dict[str, Any]:
        was_enabled = self.preference_enabled()
        self._stop_child(update_state=True, message="远程访问已暂停。")
        try:
            self._store.import_toml(source_path)
        except RemoteSettingsError as exc:
            if was_enabled:
                self._restart_best_effort()
            raise RemoteAccessError(exc.code, exc.message, status=400) from exc
        if was_enabled:
            self.start()
        else:
            self._set_state("disabled", "FRPC 配置已导入，开启后即可远程访问。")
        return self.status()

    def set_enabled(self, enabled: Any) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise RemoteAccessError(
                "invalid_remote_settings", "enabled 必须是布尔值。", status=400
            )
        return self.start() if enabled else self.stop()

    def start(self) -> dict[str, Any]:
        try:
            config = self._store.load_config()
        except RemoteSettingsError as exc:
            raise RemoteAccessError(exc.code, exc.message, status=400) from exc
        if config is None:
            raise RemoteAccessError(
                "remote_not_configured",
                "请先导入 FRPC 配置文件。",
                status=409,
            )
        try:
            self._store.save_preferences(True)
        except RemoteSettingsError as exc:
            raise RemoteAccessError(exc.code, exc.message, status=500) from exc
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return self.status()
            self._stop_event = threading.Event()
            self._retry_index = 0
            self._state = "connecting"
            self._message = "正在连接远程访问服务器..."
            worker = threading.Thread(
                target=self._run,
                name="zvec-remote-access",
                daemon=True,
            )
            self._worker = worker
            worker.start()
        return self.status()

    def start_if_enabled(self) -> None:
        """Best-effort startup restore; never blocks the desktop window."""

        try:
            if self.preference_enabled():
                self.start()
        except Exception:
            return

    def stop(self) -> dict[str, Any]:
        with suppress(RemoteSettingsError):
            self._store.save_preferences(False)
        self._stop_child(update_state=True, message="远程访问已关闭。")
        return self.status()

    def pause(self) -> None:
        """Stop the owned child without clearing the persisted preference."""

        self._stop_child(update_state=True, message="远程访问已暂停。")

    def close(self) -> None:
        self._stop_child(update_state=False, message="")

    def restart_if_enabled(self) -> None:
        self._restart_best_effort()

    # -- internal helpers ---------------------------------------------------

    def _restart_best_effort(self) -> None:
        if not self.preference_enabled():
            return
        with suppress(Exception):
            self.start()

    def _set_state(self, state: str, message: str) -> None:
        with self._lock:
            self._state = state
            self._message = message

    def _next_delay(self) -> float:
        with self._lock:
            index = min(self._retry_index, len(self._retry_delays) - 1)
            self._retry_index += 1
        return self._retry_delays[index]

    def _stop_child(self, *, update_state: bool, message: str) -> None:
        self._stop_event.set()
        with self._lock:
            worker = self._worker
            process = self._process
        # Kill the owned child first so its reader thread unblocks promptly.
        if process is not None:
            _terminate_process(process)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=_JOIN_TIMEOUT)
        with self._lock:
            still_running = worker is not None and worker.is_alive()
            remaining = self._process
            if not still_running:
                self._worker = None
                self._process = None
            if update_state:
                self._state = "disabled"
                self._message = message
        if remaining is not None and not still_running:
            _terminate_process(remaining)

    # -- worker -------------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                if self._attempt() != "exited":
                    break
                delay = self._next_delay()
                if self._stop_event.wait(delay):
                    break
        finally:
            with self._lock:
                process = self._process
                self._process = None
                if self._worker is threading.current_thread():
                    self._worker = None
                stopping = self._stop_event.is_set()
                if stopping and self._state != "error":
                    self._state = "disabled"
                    self._message = "远程访问已关闭。"
            if process is not None and stopping:
                _terminate_process(process)

    def _attempt(self) -> str:
        config = self._load_config_or_error()
        if config is None:
            return "fatal"
        try:
            host, port = self._lan_endpoint()
        except Exception as exc:
            self._set_state("error", _endpoint_message(exc))
            return "fatal"
        if not isinstance(host, str) or not host or not isinstance(port, int):
            self._set_state("error", "局域网网关地址无效，远程访问未启动。")
            return "fatal"
        if config.remote_port != port:
            self._set_state(
                "error",
                "当前局域网 HTTP 端口与远程端口不一致，请检查 FRPC 配置。",
            )
            return "fatal"
        try:
            config_path = self._store.write_config(
                config, local_host=host, local_port=port
            )
        except RemoteSettingsError:
            self._set_state("error", "无法写入 FRPC 运行配置。")
            return "fatal"
        executable = Path(self._executable())
        if not executable.is_file():
            self._set_state("error", "远程访问组件缺失，请重新安装 YaoLens。")
            return "fatal"
        if self._stop_event.is_set():
            return "stopped"
        try:
            process = self._popen(
                [str(executable), "-c", str(config_path)],
                shell=False,
                creationflags=_CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(config_path.parent),
            )
        except OSError:
            self._set_state("error", "无法启动远程访问组件。")
            return "fatal"
        with self._lock:
            self._process = process
            self._state = "connecting"
            self._message = "正在连接远程访问服务器..."
        if self._stop_event.is_set():
            with self._lock:
                self._process = None
            _terminate_process(process)
            return "stopped"
        self._read_until_exit(process, config.proxy_name)
        with self._lock:
            self._process = None
        if self._stop_event.is_set():
            return "stopped"
        self._set_state("reconnecting", "远程访问连接中断，正在重试...")
        return "exited"

    def _load_config_or_error(self) -> RemoteFrpConfig | None:
        try:
            config = self._store.load_config()
        except RemoteSettingsError:
            self._set_state("error", "已保存的 FRPC 配置无法读取。")
            return None
        if config is None:
            self._set_state("unconfigured", "尚未导入 FRPC 配置。")
            return None
        return config

    def _read_until_exit(self, process: Any, proxy_name: str) -> None:
        stream = getattr(process, "stdout", None)
        if stream is not None:
            try:
                for line in stream:
                    if self._stop_event.is_set():
                        break
                    self._handle_line(line, proxy_name)
            except (OSError, ValueError):
                pass
            finally:
                with suppress(Exception):
                    stream.close()
        _wait_process(process)

    def _handle_line(self, line: Any, proxy_name: str) -> None:
        if not isinstance(line, str):
            return
        text = line.strip()
        if not text:
            return
        if _START_PROXY_SUCCESS in text and f"[{proxy_name}]" in text:
            self._set_state("connected", "远程访问已连接，代理已注册。")
        elif f"[{proxy_name}]" in text and (
            _START_PROXY_ERROR in text or "start error:" in text
        ):
            self._set_state("reconnecting", "远程代理注册失败，正在重试...")
        elif _LOGIN_FAILED in text:
            self._set_state("connecting", "远程服务器登录失败，正在重试...")
        elif any(hint in text for hint in _RECONNECT_HINTS):
            self._set_state("reconnecting", "远程访问连接中断，正在重试...")
        elif "try to connect to server" in text:
            with self._lock:
                reconnecting = self._state in {"connected", "reconnecting"}
            if reconnecting:
                self._set_state("reconnecting", "远程访问连接中断，正在重试...")
        elif _LOGIN_SUCCESS in text:
            self._set_state("connecting", "已登录远程服务器，正在注册代理...")


def _terminate_process(process: Any) -> None:
    with suppress(Exception):
        process.terminate()
    try:
        process.wait(timeout=_TERMINATE_TIMEOUT)
        return
    except Exception:
        pass
    with suppress(Exception):
        process.kill()
    with suppress(Exception):
        process.wait(timeout=_TERMINATE_TIMEOUT)


def _wait_process(process: Any) -> None:
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return
    with suppress(Exception):
        wait(timeout=_TERMINATE_TIMEOUT)


def _endpoint_message(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return "局域网服务尚未运行，开启移动端网关后才能启动远程访问。"


def _bundled_frpc_executable() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        return base / "frp" / "frpc.exe"
    root = Path(__file__).resolve().parents[1]
    return root / "third_party" / "frp" / "windows-amd64" / "frpc.exe"


__all__ = ["RemoteAccessController", "RemoteAccessError"]
