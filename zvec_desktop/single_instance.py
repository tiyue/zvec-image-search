"""Cross-process single-instance coordination for the Python desktop.

Windows uses the same named mutex and auto-reset events as the former WPF
application.  Signals contain no payload: API keys, paths, and user data are
never placed in shared memory or kernel object names.  A background waiter only
puts enum values into a local queue; tkinter is touched exclusively by the
``TkSingleInstancePump`` on the UI thread.
"""

from __future__ import annotations

import ctypes
import importlib
import os
import queue
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, cast

SINGLE_INSTANCE_MUTEX_NAME = r"Local\Zvec.ImageSearch.Desktop"
ACTIVATE_INSTANCE_EVENT_NAME = r"Local\Zvec.ImageSearch.Desktop.Activate"
EXIT_INSTANCE_EVENT_NAME = r"Local\Zvec.ImageSearch.Desktop.Exit"
EXIT_RUNNING_INSTANCE_ARGUMENT = "--exit-running-instance"

_ERROR_FILE_NOT_FOUND = 2
_ERROR_ALREADY_EXISTS = 183
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_WAIT_FAILED = 0xFFFFFFFF
_DEFAULT_SIGNAL_TIMEOUT = 2.0
_DEFAULT_SIGNAL_RETRY = 0.05
_DEFAULT_NATIVE_WAIT_MS = 100

NativeHandle = object


class SingleInstanceError(RuntimeError):
    """Base class for structured single-instance failures."""


class SingleInstanceStateError(SingleInstanceError):
    """The coordinator was started, closed, or pumped in an invalid order."""


class SingleInstanceNativeError(SingleInstanceError):
    """A named kernel-object operation failed."""

    def __init__(self, operation: str, error_code: int, detail: str = "") -> None:
        self.operation = operation
        self.error_code = error_code
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{operation} failed with error {error_code}{suffix}")


class SingleInstanceFileLockError(SingleInstanceError):
    """The non-Windows fallback lock could not be used safely."""


class SingleInstanceCallbackError(SingleInstanceError):
    """A UI-thread activation or exit callback failed."""

    def __init__(self, signal: InstanceSignal, cause: BaseException) -> None:
        self.signal = signal
        self.cause = cause
        super().__init__(f"{signal.value} callback failed: {cause}")


class InstanceSignal(str, Enum):
    ACTIVATE = "activate"
    EXIT = "exit"


class InstanceDisposition(str, Enum):
    PRIMARY = "primary"
    ACTIVATED_EXISTING = "activated_existing"
    EXIT_REQUESTED = "exit_requested"
    SIGNAL_FAILED = "signal_failed"
    NO_RUNNING_INSTANCE = "no_running_instance"
    ALREADY_RUNNING_NO_SIGNAL = "already_running_no_signal"


@dataclass(frozen=True, slots=True)
class InstanceStartResult:
    """Startup decision matching the legacy WPF exit-code contract."""

    disposition: InstanceDisposition
    exit_code: int
    signal_sent: bool = False
    error: SingleInstanceError | None = None

    @property
    def should_run_ui(self) -> bool:
        return self.disposition is InstanceDisposition.PRIMARY


class WindowsSingleInstanceApi(Protocol):
    """Injectable boundary around the small Win32 named-object surface."""

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


class FileLockBackend(Protocol):
    """Non-Windows fallback; it intentionally has no activation data channel."""

    def acquire(self) -> bool: ...

    def close(self) -> None: ...


class _FcntlModule(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> None: ...


class TkAfterRoot(Protocol):
    """Small tkinter scheduling contract kept fakeable in unit tests."""

    def after(self, milliseconds: int, callback: Callable[[], None]) -> str: ...

    def after_cancel(self, identifier: str) -> None: ...


class SingleInstanceCoordinator:
    """Acquire the desktop instance and receive payload-free Activate/Exit events."""

    def __init__(
        self,
        *,
        native_api: WindowsSingleInstanceApi | None = None,
        file_lock: FileLockBackend | None = None,
        platform_name: str | None = None,
        signal_timeout: float = _DEFAULT_SIGNAL_TIMEOUT,
        signal_retry_interval: float = _DEFAULT_SIGNAL_RETRY,
        native_wait_ms: int = _DEFAULT_NATIVE_WAIT_MS,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        selected_platform = platform_name or os.name
        self._native: WindowsSingleInstanceApi | None
        self._file_lock: FileLockBackend | None
        if native_api is not None:
            selected_platform = "nt"
        if selected_platform == "nt":
            self._native = native_api or _WindowsNativeApi()
            self._file_lock = None
        else:
            self._native = None
            self._file_lock = file_lock or PosixFileLock(default_lock_path())
        if signal_timeout <= 0:
            raise ValueError("signal_timeout must be positive")
        if signal_retry_interval <= 0:
            raise ValueError("signal_retry_interval must be positive")
        if isinstance(native_wait_ms, bool) or native_wait_ms < 10:
            raise ValueError("native_wait_ms must be at least 10")
        self._signal_timeout = signal_timeout
        self._signal_retry_interval = signal_retry_interval
        self._native_wait_ms = native_wait_ms
        self._clock = clock
        self._sleeper = sleeper

        self._lifecycle_lock = threading.RLock()
        self._background_error_lock = threading.Lock()
        self._notifications: queue.SimpleQueue[InstanceSignal] = queue.SimpleQueue()
        self._background_error: SingleInstanceError | None = None
        self._stop_waiter = threading.Event()
        self._waiter: threading.Thread | None = None
        self._mutex: NativeHandle | None = None
        self._activate_event: NativeHandle | None = None
        self._exit_event: NativeHandle | None = None
        self._owns_mutex = False
        self._file_lock_acquired = False
        self._started = False
        self._closed = False

    @property
    def is_primary(self) -> bool:
        return self._owns_mutex or self._file_lock_acquired

    @property
    def is_closed(self) -> bool:
        return self._closed

    def start(self, *, request_existing_exit: bool = False) -> InstanceStartResult:
        """Acquire the primary role or notify the already-running application."""

        with self._lifecycle_lock:
            if self._closed:
                raise SingleInstanceStateError("Coordinator has already been closed.")
            if self._started:
                raise SingleInstanceStateError("Coordinator has already been started.")
            self._started = True
            if self._native is None:
                return self._start_file_lock(request_existing_exit)
            return self._start_windows(request_existing_exit)

    def drain_notifications(self, *, maximum: int = 64) -> tuple[InstanceSignal, ...]:
        """Drain local enum notifications; call this only from the UI thread."""

        if isinstance(maximum, bool) or maximum < 1:
            raise ValueError("maximum must be a positive integer")
        notifications: list[InstanceSignal] = []
        for _ in range(maximum):
            try:
                notifications.append(self._notifications.get_nowait())
            except queue.Empty:
                break
        return tuple(notifications)

    def take_background_error(self) -> SingleInstanceError | None:
        with self._background_error_lock:
            error = self._background_error
            self._background_error = None
            return error

    def close(self) -> None:
        """Stop the bounded waiter before releasing any native handles."""

        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._stop_waiter.set()
            waiter = self._waiter
        if waiter is not None and waiter is not threading.current_thread():
            waiter.join(timeout=max(1.0, self._native_wait_ms / 1000.0 + 0.5))
            if waiter.is_alive():
                # Closing a handle while Win32 is waiting on it is unsafe. Leave
                # the handles process-owned and report a structured late error.
                self._store_background_error(
                    SingleInstanceStateError(
                        "Native instance waiter did not stop before shutdown."
                    )
                )
                return
        with self._lifecycle_lock:
            self._waiter = None
            if self._native is not None:
                self._close_windows_handles()
            elif self._file_lock is not None:
                self._file_lock.close()
                self._file_lock_acquired = False

    def __enter__(self) -> SingleInstanceCoordinator:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _start_windows(self, request_existing_exit: bool) -> InstanceStartResult:
        assert self._native is not None
        try:
            mutex, created_new = self._native.create_mutex(
                SINGLE_INSTANCE_MUTEX_NAME,
                True,
            )
            self._mutex = mutex
        except SingleInstanceError:
            self._started = False
            raise
        if not created_new:
            event_name = (
                EXIT_INSTANCE_EVENT_NAME
                if request_existing_exit
                else ACTIVATE_INSTANCE_EVENT_NAME
            )
            sent, error = self._signal_existing(event_name)
            self._close_secondary_mutex()
            if sent:
                return InstanceStartResult(
                    disposition=(
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

        self._owns_mutex = True
        if request_existing_exit:
            self._close_windows_handles()
            return InstanceStartResult(
                InstanceDisposition.NO_RUNNING_INSTANCE,
                exit_code=3,
            )
        try:
            self._activate_event = self._native.create_auto_reset_event(
                ACTIVATE_INSTANCE_EVENT_NAME
            )
            self._exit_event = self._native.create_auto_reset_event(
                EXIT_INSTANCE_EVENT_NAME
            )
            self._waiter = threading.Thread(
                target=self._wait_for_windows_signals,
                name="zvec-single-instance",
                daemon=True,
            )
            self._waiter.start()
        except BaseException:
            self._close_windows_handles()
            raise
        return InstanceStartResult(InstanceDisposition.PRIMARY, exit_code=0)

    def _start_file_lock(self, request_existing_exit: bool) -> InstanceStartResult:
        assert self._file_lock is not None
        try:
            acquired = self._file_lock.acquire()
        except SingleInstanceError:
            self._started = False
            raise
        if acquired:
            self._file_lock_acquired = True
            if request_existing_exit:
                self._file_lock.close()
                self._file_lock_acquired = False
                return InstanceStartResult(
                    InstanceDisposition.NO_RUNNING_INSTANCE,
                    exit_code=3,
                )
            return InstanceStartResult(InstanceDisposition.PRIMARY, exit_code=0)
        return InstanceStartResult(
            InstanceDisposition.ALREADY_RUNNING_NO_SIGNAL,
            exit_code=2,
            error=SingleInstanceFileLockError(
                "Another instance is running; activation signaling is unavailable "
                "on this platform."
            ),
        )

    def _signal_existing(
        self, event_name: str
    ) -> tuple[bool, SingleInstanceError | None]:
        assert self._native is not None
        deadline = self._clock() + self._signal_timeout
        last_error: SingleInstanceError | None = None
        while True:
            handle: NativeHandle | None = None
            try:
                handle = self._native.open_event(event_name)
                if handle is not None:
                    self._native.signal_event(handle)
                    return True, None
            except SingleInstanceError as exc:
                last_error = exc
                return False, last_error
            finally:
                if handle is not None:
                    try:
                        self._native.close_handle(handle)
                    except SingleInstanceError as exc:
                        last_error = last_error or exc
                        self._store_background_error(exc)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False, last_error or SingleInstanceNativeError(
                    "OpenEventW",
                    _ERROR_FILE_NOT_FOUND,
                    f"named event was unavailable after {self._signal_timeout:g}s",
                )
            self._sleeper(min(self._signal_retry_interval, remaining))

    def _wait_for_windows_signals(self) -> None:
        assert self._native is not None
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
                selected = self._native.wait_any(handles, self._native_wait_ms)
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

    def _store_background_error(self, error: SingleInstanceError) -> None:
        with self._background_error_lock:
            if self._background_error is None:
                self._background_error = error

    def _close_secondary_mutex(self) -> None:
        assert self._native is not None
        mutex = self._mutex
        self._mutex = None
        if mutex is not None:
            try:
                self._native.close_handle(mutex)
            except SingleInstanceError as exc:
                self._store_background_error(exc)

    def _close_windows_handles(self) -> None:
        assert self._native is not None
        for attribute in ("_activate_event", "_exit_event"):
            handle = getattr(self, attribute)
            setattr(self, attribute, None)
            if handle is not None:
                try:
                    self._native.close_handle(handle)
                except SingleInstanceError as exc:
                    self._store_background_error(exc)
        mutex = self._mutex
        self._mutex = None
        if mutex is not None:
            if self._owns_mutex:
                try:
                    self._native.release_mutex(mutex)
                except SingleInstanceError as exc:
                    self._store_background_error(exc)
            try:
                self._native.close_handle(mutex)
            except SingleInstanceError as exc:
                self._store_background_error(exc)
        self._owns_mutex = False


class TkSingleInstancePump:
    """Poll coordinator notifications using ``root.after`` on the Tk thread."""

    def __init__(
        self,
        root: TkAfterRoot,
        coordinator: SingleInstanceCoordinator,
        *,
        on_activate: Callable[[], None],
        on_exit: Callable[[], None],
        on_error: Callable[[SingleInstanceError], None] | None = None,
        poll_interval_ms: int = 50,
    ) -> None:
        if isinstance(poll_interval_ms, bool) or poll_interval_ms < 10:
            raise ValueError("poll_interval_ms must be at least 10")
        self._root = root
        self._coordinator = coordinator
        self._on_activate = on_activate
        self._on_exit = on_exit
        self._on_error = on_error
        self._poll_interval_ms = poll_interval_ms
        self._after_handle: str | None = None
        self._running = False
        self.last_error: SingleInstanceError | None = None

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._schedule_next()

    def stop(self) -> None:
        self._running = False
        handle = self._after_handle
        self._after_handle = None
        if handle is not None:
            # Tk may already be destroyed. No native handle is touched here.
            with suppress(Exception):
                self._root.after_cancel(handle)

    def _poll(self) -> None:
        self._after_handle = None
        if not self._running:
            return
        if self._coordinator.is_closed:
            self._running = False
            return
        background_error = self._coordinator.take_background_error()
        if background_error is not None:
            self._report_error(background_error)
        for signal in self._coordinator.drain_notifications():
            callback = (
                self._on_activate
                if signal is InstanceSignal.ACTIVATE
                else self._on_exit
            )
            try:
                callback()
            except Exception as exc:
                self._report_error(SingleInstanceCallbackError(signal, exc))
        if self._running and not self._coordinator.is_closed:
            self._schedule_next()
        elif self._coordinator.is_closed:
            self._running = False

    def _schedule_next(self) -> None:
        try:
            self._after_handle = self._root.after(
                self._poll_interval_ms,
                self._poll,
            )
        except Exception as exc:
            self._after_handle = None
            self._running = False
            self._report_error(
                SingleInstanceStateError(f"Unable to schedule Tk polling: {exc}")
            )

    def _report_error(self, error: SingleInstanceError) -> None:
        self.last_error = error
        if self._on_error is None:
            return
        # Error reporting itself must not break Tk's event loop.
        with suppress(Exception):
            self._on_error(error)


class _WindowsNativeApi:
    """ctypes implementation with no Python callback crossing the Win32 boundary."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows named objects are only available on Windows.")
        try:
            kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise SingleInstanceNativeError(
                "LoadLibrary(Kernel32)", -1, str(exc)
            ) from exc
        self._create_mutex = kernel32.CreateMutexW
        self._create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._create_mutex.restype = wintypes.HANDLE
        self._release_mutex = kernel32.ReleaseMutex
        self._release_mutex.argtypes = [wintypes.HANDLE]
        self._release_mutex.restype = wintypes.BOOL
        self._create_event = kernel32.CreateEventW
        self._create_event.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self._create_event.restype = wintypes.HANDLE
        self._open_event = kernel32.OpenEventW
        self._open_event.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self._open_event.restype = wintypes.HANDLE
        self._set_event = kernel32.SetEvent
        self._set_event.argtypes = [wintypes.HANDLE]
        self._set_event.restype = wintypes.BOOL
        self._wait_multiple = kernel32.WaitForMultipleObjects
        self._wait_multiple.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self._wait_multiple.restype = wintypes.DWORD
        self._close = kernel32.CloseHandle
        self._close.argtypes = [wintypes.HANDLE]
        self._close.restype = wintypes.BOOL

    def create_mutex(
        self, name: str, initially_owned: bool
    ) -> tuple[NativeHandle, bool]:
        ctypes.set_last_error(0)
        handle = self._create_mutex(None, initially_owned, name)
        error = ctypes.get_last_error()
        if not handle:
            raise SingleInstanceNativeError("CreateMutexW", error)
        return int(handle), error != _ERROR_ALREADY_EXISTS

    def release_mutex(self, handle: NativeHandle) -> None:
        if not self._release_mutex(_as_windows_handle(handle)):
            raise SingleInstanceNativeError("ReleaseMutex", ctypes.get_last_error())

    def create_auto_reset_event(self, name: str) -> NativeHandle:
        handle = self._create_event(None, False, False, name)
        if not handle:
            raise SingleInstanceNativeError("CreateEventW", ctypes.get_last_error())
        return int(handle)

    def open_event(self, name: str) -> NativeHandle | None:
        handle = self._open_event(_EVENT_MODIFY_STATE, False, name)
        if handle:
            return int(handle)
        error = ctypes.get_last_error()
        if error == _ERROR_FILE_NOT_FOUND:
            return None
        raise SingleInstanceNativeError("OpenEventW", error)

    def signal_event(self, handle: NativeHandle) -> None:
        if not self._set_event(_as_windows_handle(handle)):
            raise SingleInstanceNativeError("SetEvent", ctypes.get_last_error())

    def wait_any(self, handles: Sequence[NativeHandle], timeout_ms: int) -> int | None:
        if not handles:
            raise ValueError("handles must not be empty")
        array_type = wintypes.HANDLE * len(handles)
        native_handles = array_type(*(_as_windows_handle(item) for item in handles))
        result = int(
            self._wait_multiple(len(handles), native_handles, False, timeout_ms)
        )
        if result == _WAIT_TIMEOUT:
            return None
        if result == _WAIT_FAILED:
            raise SingleInstanceNativeError(
                "WaitForMultipleObjects",
                ctypes.get_last_error(),
            )
        index = result - _WAIT_OBJECT_0
        if 0 <= index < len(handles):
            return index
        raise SingleInstanceNativeError(
            "WaitForMultipleObjects",
            result,
            "unexpected wait result",
        )

    def close_handle(self, handle: NativeHandle) -> None:
        if not self._close(_as_windows_handle(handle)):
            raise SingleInstanceNativeError("CloseHandle", ctypes.get_last_error())


class PosixFileLock:
    """Zero-byte, owner-only flock fallback without an activation data channel."""

    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().absolute()
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        fcntl = _load_fcntl()
        parent = self._path.parent
        descriptor: int | None = None
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = parent.lstat()
            user_id = _current_user_id()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise SingleInstanceFileLockError(
                    f"Single-instance lock directory is unsafe: {parent}"
                )
            if user_id is not None and metadata.st_uid != user_id:
                raise SingleInstanceFileLockError(
                    f"Single-instance lock directory has another owner: {parent}"
                )
            parent.chmod(0o700)
            if not hasattr(os, "O_NOFOLLOW"):
                raise SingleInstanceFileLockError(
                    "This platform cannot safely reject symbolic-link lock files."
                )
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= os.O_NOFOLLOW
            descriptor = os.open(self._path, flags, 0o600)
            file_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(file_metadata.st_mode):
                raise SingleInstanceFileLockError(
                    f"Single-instance lock is not a regular file: {self._path}"
                )
            if user_id is not None and file_metadata.st_uid != user_id:
                raise SingleInstanceFileLockError(
                    f"Single-instance lock has another owner: {self._path}"
                )
            if file_metadata.st_size != 0:
                raise SingleInstanceFileLockError(
                    f"Single-instance lock must remain empty: {self._path}"
                )
            _secure_descriptor_permissions(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                descriptor = None
                return False
            self._descriptor = descriptor
            return True
        except SingleInstanceError:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            raise SingleInstanceFileLockError(
                f"Unable to acquire single-instance lock: {self._path}"
            ) from exc

    def close(self) -> None:
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            fcntl = _load_fcntl()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except (OSError, SingleInstanceFileLockError):
            pass
        finally:
            os.close(descriptor)


def default_lock_path() -> Path:
    """Return an owner-specific path; the lock file remains empty."""

    runtime = os.getenv("XDG_RUNTIME_DIR", "").strip()
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    user_id = _current_user_id()
    owner = str(user_id) if user_id is not None else "user"
    return base / f"zvec-image-search-{owner}" / "desktop.lock"


def _load_fcntl() -> _FcntlModule:
    try:
        module = importlib.import_module("fcntl")
    except ImportError as exc:
        raise SingleInstanceFileLockError(
            "This platform has no supported file-lock implementation."
        ) from exc
    return cast(_FcntlModule, module)


def _current_user_id() -> int | None:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        return None
    return int(getuid())


def _secure_descriptor_permissions(descriptor: int) -> None:
    fchmod = getattr(os, "fchmod", None)
    if not callable(fchmod):
        raise SingleInstanceFileLockError(
            "This platform cannot secure the single-instance lock permissions."
        )
    fchmod(descriptor, 0o600)


def _as_windows_handle(handle: NativeHandle) -> wintypes.HANDLE:
    if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
        raise SingleInstanceNativeError("HANDLE conversion", -1, "invalid handle")
    return wintypes.HANDLE(handle)


__all__ = [
    "ACTIVATE_INSTANCE_EVENT_NAME",
    "EXIT_INSTANCE_EVENT_NAME",
    "EXIT_RUNNING_INSTANCE_ARGUMENT",
    "FileLockBackend",
    "InstanceDisposition",
    "InstanceSignal",
    "InstanceStartResult",
    "PosixFileLock",
    "SINGLE_INSTANCE_MUTEX_NAME",
    "SingleInstanceCallbackError",
    "SingleInstanceCoordinator",
    "SingleInstanceError",
    "SingleInstanceFileLockError",
    "SingleInstanceNativeError",
    "SingleInstanceStateError",
    "TkAfterRoot",
    "TkSingleInstancePump",
    "WindowsSingleInstanceApi",
    "default_lock_path",
]
