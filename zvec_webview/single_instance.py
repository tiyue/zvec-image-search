"""Windows session-scoped single-instance coordination for the WebView host."""

from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
import queue
import threading
import time
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

_ERROR_FILE_NOT_FOUND = 2
_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_DEFAULT_SIGNAL_TIMEOUT = 2.0
_DEFAULT_SIGNAL_RETRY_INTERVAL = 0.05
_DEFAULT_NATIVE_WAIT_MS = 100
_SCOPE_DIGEST_HEX_CHARS = 24
_INSTANCE_NAMESPACE = r"Local\Zvec.ImageSearch.WebView"

NativeHandle = object

# Non-Windows typeshed stubs intentionally omit these Windows-only attributes.
_WINDOWS_CTYPES = cast(Any, ctypes)


class SingleInstanceError(RuntimeError):
    """Base class for structured single-instance failures."""


class SingleInstanceStateError(SingleInstanceError):
    """The coordinator was used in an invalid lifecycle state."""


class SingleInstanceUnsupportedError(SingleInstanceError):
    """The native coordinator was requested on a non-Windows platform."""


class SingleInstanceNativeError(SingleInstanceError):
    """A Win32 named-object operation failed."""

    def __init__(self, operation: str, error_code: int, detail: str = "") -> None:
        self.operation = operation
        self.error_code = error_code
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{operation} failed with error {error_code}{suffix}")


class InstanceSignal(str, Enum):
    """Payload-free commands accepted from another WebView process."""

    ACTIVATE = "activate"
    EXIT = "exit"


class InstanceDisposition(str, Enum):
    """Startup decision for the process that called ``start``."""

    PRIMARY = "primary"
    ACTIVATED_EXISTING = "activated_existing"
    EXIT_REQUESTED = "exit_requested"
    SIGNAL_FAILED = "signal_failed"
    NO_RUNNING_INSTANCE = "no_running_instance"


@dataclass(frozen=True, slots=True)
class InstanceStartResult:
    """Result of acquiring the primary role or signalling an existing host."""

    disposition: InstanceDisposition
    exit_code: int
    signal_sent: bool = False
    error: SingleInstanceError | None = None

    @property
    def should_run_ui(self) -> bool:
        return self.disposition is InstanceDisposition.PRIMARY


@dataclass(frozen=True, slots=True)
class InstanceObjectNames:
    """Non-sensitive names for one normalized configuration scope."""

    scope_digest: str
    mutex: str
    activate_event: str
    exit_event: str


class WindowsSingleInstanceApi(Protocol):
    """Injectable boundary around the required Win32 kernel functions."""

    def create_mutex(
        self, name: str, initially_owned: bool
    ) -> tuple[NativeHandle, bool]: ...

    def release_mutex(self, handle: NativeHandle) -> None: ...

    def create_auto_reset_event(self, name: str) -> NativeHandle: ...

    def open_event(self, name: str) -> NativeHandle | None: ...

    def signal_event(self, handle: NativeHandle) -> None: ...

    def wait_any(
        self, handles: Sequence[NativeHandle], timeout_ms: int
    ) -> int | None: ...

    def close_handle(self, handle: NativeHandle) -> None: ...


def instance_object_names(config_scope: str | Path) -> InstanceObjectNames:
    r"""Build ``Local\`` object names without exposing the configuration path."""

    normalized = _normalize_config_scope(config_scope)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[
        :_SCOPE_DIGEST_HEX_CHARS
    ]
    stem = f"{_INSTANCE_NAMESPACE}.{digest}"
    return InstanceObjectNames(
        scope_digest=digest,
        mutex=f"{stem}.Mutex",
        activate_event=f"{stem}.Activate",
        exit_event=f"{stem}.Exit",
    )


class SingleInstanceCoordinator:
    """Own one WebView instance and queue payload-free cross-process signals."""

    def __init__(
        self,
        config_scope: str | Path,
        *,
        native_api: WindowsSingleInstanceApi | None = None,
        platform_name: str | None = None,
        signal_timeout: float = _DEFAULT_SIGNAL_TIMEOUT,
        signal_retry_interval: float = _DEFAULT_SIGNAL_RETRY_INTERVAL,
        native_wait_ms: int = _DEFAULT_NATIVE_WAIT_MS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if signal_timeout <= 0:
            raise ValueError("signal_timeout must be positive")
        if signal_retry_interval <= 0:
            raise ValueError("signal_retry_interval must be positive")
        if (
            isinstance(native_wait_ms, bool)
            or not isinstance(native_wait_ms, int)
            or native_wait_ms < 10
        ):
            raise ValueError("native_wait_ms must be an integer of at least 10")

        selected_platform = platform_name or os.name
        if native_api is not None:
            selected_platform = "nt"
        self.names = instance_object_names(config_scope)
        self._native = (
            native_api
            if native_api is not None
            else _WindowsNativeApi()
            if selected_platform == "nt"
            else None
        )
        self._signal_timeout = float(signal_timeout)
        self._signal_retry_interval = float(signal_retry_interval)
        self._native_wait_ms = native_wait_ms
        self._clock = clock
        self._sleeper = sleeper

        self._lifecycle_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._background_error_lock = threading.Lock()
        self._notifications: queue.SimpleQueue[InstanceSignal] = queue.SimpleQueue()
        self._stop_waiter = threading.Event()
        self._waiter: threading.Thread | None = None
        self._background_error: SingleInstanceError | None = None
        self._mutex: NativeHandle | None = None
        self._activate_event: NativeHandle | None = None
        self._exit_event: NativeHandle | None = None
        self._owns_mutex = False
        self._started = False
        self._closed = False
        self._cleanup_complete = False

    @property
    def is_primary(self) -> bool:
        return self._owns_mutex

    @property
    def is_closed(self) -> bool:
        return self._closed

    def start(self, *, request_existing_exit: bool = False) -> InstanceStartResult:
        """Acquire the primary role or notify the already-running host."""

        if not isinstance(request_existing_exit, bool):
            raise ValueError("request_existing_exit must be a boolean")
        native = self._native
        if native is None:
            raise SingleInstanceUnsupportedError(
                "WebView single-instance coordination requires Windows."
            )

        with self._lifecycle_lock:
            if self._closed:
                raise SingleInstanceStateError("Coordinator has already been closed.")
            if self._started:
                raise SingleInstanceStateError("Coordinator has already been started.")
            self._started = True
            try:
                mutex, created_new = native.create_mutex(
                    self.names.mutex,
                    True,
                )
            except BaseException:
                self._started = False
                raise
            self._mutex = mutex

            if not created_new:
                return self._signal_secondary_locked(request_existing_exit)

            self._owns_mutex = True
            if request_existing_exit:
                self._cleanup_primary_handles_locked()
                return InstanceStartResult(
                    InstanceDisposition.NO_RUNNING_INSTANCE,
                    exit_code=3,
                )

            try:
                self._activate_event = native.create_auto_reset_event(
                    self.names.activate_event
                )
                self._exit_event = native.create_auto_reset_event(self.names.exit_event)
                waiter = threading.Thread(
                    target=self._wait_for_signals,
                    name=f"zvec-webview-instance-{self.names.scope_digest[:8]}",
                    daemon=True,
                )
                self._waiter = waiter
                waiter.start()
            except BaseException:
                self._cleanup_primary_handles_locked()
                raise
            return InstanceStartResult(InstanceDisposition.PRIMARY, exit_code=0)

    def drain_notifications(self, *, maximum: int = 64) -> tuple[InstanceSignal, ...]:
        """Drain queued signals without calling any window or GUI API."""

        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
            raise ValueError("maximum must be a positive integer")
        notifications: list[InstanceSignal] = []
        for _ in range(maximum):
            try:
                notifications.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return tuple(notifications)

    def take_background_error(self) -> SingleInstanceError | None:
        """Return and clear a waiter or cleanup error."""

        with self._background_error_lock:
            error = self._background_error
            self._background_error = None
            return error

    def close(self) -> None:
        """Stop the waiter before releasing named handles; repeated calls are safe."""

        with self._close_lock:
            with self._lifecycle_lock:
                if self._cleanup_complete:
                    self._closed = True
                    return
                self._closed = True
                self._stop_waiter.set()
                waiter = self._waiter

            if waiter is not None and waiter is not threading.current_thread():
                waiter.join(timeout=max(1.0, self._native_wait_ms / 1000.0 + 0.5))
                if waiter.is_alive():
                    self._store_background_error(
                        SingleInstanceStateError(
                            "Native instance waiter did not stop before cleanup."
                        )
                    )
                    return

            with self._lifecycle_lock:
                self._waiter = None
                self._cleanup_primary_handles_locked()
                self._cleanup_complete = True

    def __enter__(self) -> SingleInstanceCoordinator:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _signal_secondary_locked(
        self, request_existing_exit: bool
    ) -> InstanceStartResult:
        event_name = (
            self.names.exit_event
            if request_existing_exit
            else self.names.activate_event
        )
        sent, error = self._signal_existing(event_name)
        self._close_secondary_mutex_locked()
        if sent:
            return InstanceStartResult(
                (
                    InstanceDisposition.EXIT_REQUESTED
                    if request_existing_exit
                    else InstanceDisposition.ACTIVATED_EXISTING
                ),
                exit_code=0,
                signal_sent=True,
            )
        return InstanceStartResult(
            InstanceDisposition.SIGNAL_FAILED,
            exit_code=2,
            error=error,
        )

    def _signal_existing(
        self, event_name: str
    ) -> tuple[bool, SingleInstanceError | None]:
        native = self._required_native()
        deadline = self._clock() + self._signal_timeout
        last_error: SingleInstanceError | None = None
        while True:
            event: NativeHandle | None = None
            try:
                event = native.open_event(event_name)
                if event is not None:
                    native.signal_event(event)
                    return True, None
            except SingleInstanceError as exc:
                last_error = exc
                return False, exc
            finally:
                if event is not None:
                    try:
                        native.close_handle(event)
                    except SingleInstanceError as exc:
                        self._store_background_error(exc)

            remaining = deadline - self._clock()
            if remaining <= 0:
                return False, last_error or SingleInstanceNativeError(
                    "OpenEventW",
                    _ERROR_FILE_NOT_FOUND,
                    f"named event was unavailable after {self._signal_timeout:g}s",
                )
            self._sleeper(min(self._signal_retry_interval, remaining))

    def _wait_for_signals(self) -> None:
        native = self._required_native()
        activate = self._activate_event
        exit_event = self._exit_event
        if activate is None or exit_event is None:
            self._store_background_error(
                SingleInstanceStateError("Named events were not initialized.")
            )
            return
        handles = (activate, exit_event)
        while not self._stop_waiter.is_set():
            try:
                selected = native.wait_any(handles, self._native_wait_ms)
            except SingleInstanceError as exc:
                self._store_background_error(exc)
                return
            if selected is None:
                continue
            if selected == 0:
                self._notifications.put(InstanceSignal.ACTIVATE)
            elif selected == 1:
                self._notifications.put(InstanceSignal.EXIT)
            else:
                self._store_background_error(
                    SingleInstanceNativeError(
                        "WaitForMultipleObjects",
                        -1,
                        f"unexpected index {selected}",
                    )
                )
                return

    def _close_secondary_mutex_locked(self) -> None:
        native = self._required_native()
        mutex = self._mutex
        self._mutex = None
        if mutex is None:
            return
        try:
            native.close_handle(mutex)
        except SingleInstanceError as exc:
            self._store_background_error(exc)

    def _cleanup_primary_handles_locked(self) -> None:
        native = self._native
        if native is None:
            return
        for attribute in ("_activate_event", "_exit_event"):
            handle = getattr(self, attribute)
            setattr(self, attribute, None)
            if handle is not None:
                try:
                    native.close_handle(handle)
                except SingleInstanceError as exc:
                    self._store_background_error(exc)

        mutex = self._mutex
        self._mutex = None
        if mutex is not None:
            if self._owns_mutex:
                try:
                    native.release_mutex(mutex)
                except SingleInstanceError as exc:
                    self._store_background_error(exc)
            try:
                native.close_handle(mutex)
            except SingleInstanceError as exc:
                self._store_background_error(exc)
        self._owns_mutex = False

    def _required_native(self) -> WindowsSingleInstanceApi:
        native = self._native
        if native is None:
            raise SingleInstanceUnsupportedError(
                "WebView single-instance coordination requires Windows."
            )
        return native

    def _store_background_error(self, error: SingleInstanceError) -> None:
        with self._background_error_lock:
            if self._background_error is None:
                self._background_error = error


class _WindowsNativeApi:
    """Small lazy Win32 binding that is safe to import on non-Windows hosts."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise SingleInstanceUnsupportedError(
                "Win32 named objects are unavailable on this platform."
            )
        kernel32 = _WINDOWS_CTYPES.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        self._create_mutex = kernel32.CreateMutexW
        self._create_mutex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        self._create_mutex.restype = ctypes.c_void_p
        self._release_mutex = kernel32.ReleaseMutex
        self._release_mutex.argtypes = [ctypes.c_void_p]
        self._release_mutex.restype = ctypes.c_int
        self._create_event = kernel32.CreateEventW
        self._create_event.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        self._create_event.restype = ctypes.c_void_p
        self._open_event = kernel32.OpenEventW
        self._open_event.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_wchar_p,
        ]
        self._open_event.restype = ctypes.c_void_p
        self._set_event = kernel32.SetEvent
        self._set_event.argtypes = [ctypes.c_void_p]
        self._set_event.restype = ctypes.c_int
        self._wait_multiple = kernel32.WaitForMultipleObjects
        self._wait_multiple.argtypes = [
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        self._wait_multiple.restype = ctypes.c_uint32
        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [ctypes.c_void_p]
        self._close_handle.restype = ctypes.c_int

    def create_mutex(
        self, name: str, initially_owned: bool
    ) -> tuple[NativeHandle, bool]:
        _WINDOWS_CTYPES.set_last_error(0)
        handle = self._create_mutex(None, bool(initially_owned), name)
        error_code = _WINDOWS_CTYPES.get_last_error()
        if not handle:
            raise SingleInstanceNativeError("CreateMutexW", error_code)
        return handle, error_code != _ERROR_ALREADY_EXISTS

    def release_mutex(self, handle: NativeHandle) -> None:
        if not self._release_mutex(handle):
            raise SingleInstanceNativeError(
                "ReleaseMutex",
                _WINDOWS_CTYPES.get_last_error(),
            )

    def create_auto_reset_event(self, name: str) -> NativeHandle:
        handle = self._create_event(None, False, False, name)
        if not handle:
            raise SingleInstanceNativeError(
                "CreateEventW",
                _WINDOWS_CTYPES.get_last_error(),
            )
        return handle

    def open_event(self, name: str) -> NativeHandle | None:
        _WINDOWS_CTYPES.set_last_error(0)
        handle = self._open_event(_EVENT_MODIFY_STATE, False, name)
        if handle:
            return handle
        error_code = _WINDOWS_CTYPES.get_last_error()
        if error_code == _ERROR_FILE_NOT_FOUND:
            return None
        raise SingleInstanceNativeError("OpenEventW", error_code)

    def signal_event(self, handle: NativeHandle) -> None:
        if not self._set_event(handle):
            raise SingleInstanceNativeError(
                "SetEvent",
                _WINDOWS_CTYPES.get_last_error(),
            )

    def wait_any(self, handles: Sequence[NativeHandle], timeout_ms: int) -> int | None:
        native_handles = (ctypes.c_void_p * len(handles))(
            *(ctypes.c_void_p(cast(int, handle)) for handle in handles)
        )
        result = int(
            self._wait_multiple(
                len(handles),
                native_handles,
                False,
                timeout_ms,
            )
        )
        if result == _WAIT_TIMEOUT:
            return None
        if result == _WAIT_FAILED:
            raise SingleInstanceNativeError(
                "WaitForMultipleObjects",
                _WINDOWS_CTYPES.get_last_error(),
            )
        selected = result - _WAIT_OBJECT_0
        if not 0 <= selected < len(handles):
            raise SingleInstanceNativeError(
                "WaitForMultipleObjects",
                result,
                "unexpected wait result",
            )
        return selected

    def close_handle(self, handle: NativeHandle) -> None:
        if not self._close_handle(handle):
            raise SingleInstanceNativeError(
                "CloseHandle",
                _WINDOWS_CTYPES.get_last_error(),
            )


def _normalize_config_scope(config_scope: str | Path) -> str:
    raw = os.fspath(config_scope)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("config_scope must be a non-empty path")
    expanded = os.path.expanduser(raw.strip())
    absolute = expanded if ntpath.isabs(expanded) else str(Path(expanded).absolute())
    normalized = ntpath.normcase(ntpath.normpath(absolute))
    return unicodedata.normalize("NFC", normalized).casefold()


__all__ = [
    "InstanceDisposition",
    "InstanceObjectNames",
    "InstanceSignal",
    "InstanceStartResult",
    "SingleInstanceCoordinator",
    "SingleInstanceError",
    "SingleInstanceNativeError",
    "SingleInstanceStateError",
    "SingleInstanceUnsupportedError",
    "WindowsSingleInstanceApi",
    "instance_object_names",
]
