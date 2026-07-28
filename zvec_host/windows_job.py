"""Minimal Windows Job Object boundary for backend process containment.

The module is safe to import on non-Windows platforms.  Win32 libraries are
loaded only when ``create_kill_on_close_job`` is called on Windows.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
from contextlib import suppress
from typing import Any, Protocol

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class WindowsJobError(RuntimeError):
    """A Win32 Job Object operation failed."""

    def __init__(
        self,
        operation: str,
        *,
        winerror: int | None = None,
        detail: str | None = None,
    ) -> None:
        self.operation = operation
        self.winerror = winerror
        self.detail = detail or "unknown Windows error"
        suffix = f" (Windows error {winerror})" if winerror is not None else ""
        super().__init__(f"{operation} failed{suffix}: {self.detail}")


class _WindowsJobApi(Protocol):
    def create(self) -> int: ...

    def enable_kill_on_close(self, handle: int) -> None: ...

    def assign(self, job_handle: int, process_handle: int) -> None: ...

    def close(self, handle: int) -> None: ...


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _CtypesWindowsJobApi:
    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise WindowsJobError(
                "LoadLibrary",
                detail="ctypes.WinDLL is unavailable",
            )
        try:
            kernel32 = loader("kernel32.dll", use_last_error=True)
        except OSError as exc:
            raise WindowsJobError("LoadLibrary", detail=str(exc)) from exc

        self._create_job = kernel32.CreateJobObjectW
        self._create_job.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self._create_job.restype = ctypes.c_void_p

        self._set_information = kernel32.SetInformationJobObject
        self._set_information.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._set_information.restype = ctypes.c_int

        self._assign_process = kernel32.AssignProcessToJobObject
        self._assign_process.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._assign_process.restype = ctypes.c_int

        self._close_handle = kernel32.CloseHandle
        self._close_handle.argtypes = [ctypes.c_void_p]
        self._close_handle.restype = ctypes.c_int

    def create(self) -> int:
        handle = self._create_job(None, None)
        if not handle:
            raise _last_error("CreateJobObjectW")
        return int(handle)

    def enable_kill_on_close(self, handle: int) -> None:
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not self._set_information(
            ctypes.c_void_p(handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise _last_error("SetInformationJobObject")

    def assign(self, job_handle: int, process_handle: int) -> None:
        if not self._assign_process(
            ctypes.c_void_p(job_handle),
            ctypes.c_void_p(process_handle),
        ):
            raise _last_error("AssignProcessToJobObject")

    def close(self, handle: int) -> None:
        if not self._close_handle(ctypes.c_void_p(handle)):
            raise _last_error("CloseHandle")


class WindowsKillOnCloseJob:
    """Own one configured Job Object handle until explicitly closed."""

    def __init__(self, handle: int, api: _WindowsJobApi) -> None:
        self._handle: int | None = handle
        self._api = api
        self._lock = threading.Lock()

    @classmethod
    def create(
        cls,
        *,
        api: _WindowsJobApi | None = None,
    ) -> WindowsKillOnCloseJob:
        if os.name != "nt" and api is None:
            raise WindowsJobError(
                "CreateJobObjectW",
                detail="Windows Job Objects are unavailable on this platform",
            )
        resolved_api = api or _CtypesWindowsJobApi()
        handle = resolved_api.create()
        try:
            resolved_api.enable_kill_on_close(handle)
        except BaseException:
            with suppress(WindowsJobError):
                resolved_api.close(handle)
            raise
        return cls(handle, resolved_api)

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._handle is None

    def assign(self, process: subprocess.Popen[Any]) -> None:
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise WindowsJobError(
                "AssignProcessToJobObject",
                detail="the subprocess has no Windows process handle",
            )
        with self._lock:
            handle = self._handle
            if handle is None:
                raise WindowsJobError(
                    "AssignProcessToJobObject",
                    detail="the Job Object handle is already closed",
                )
            self._api.assign(handle, int(process_handle))

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            if handle is None:
                return
            self._api.close(handle)
            self._handle = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            return


def create_kill_on_close_job() -> WindowsKillOnCloseJob:
    """Create an unnamed Job Object that kills members when its handle closes."""

    return WindowsKillOnCloseJob.create()


def _last_error(operation: str) -> WindowsJobError:
    getter = getattr(ctypes, "get_last_error", None)
    code = int(getter()) if getter is not None else 0
    formatter = getattr(ctypes, "FormatError", None)
    detail = (
        str(formatter(code)).strip()
        if formatter is not None and code
        else "unknown Windows error"
    )
    return WindowsJobError(
        operation,
        winerror=code or None,
        detail=detail,
    )


__all__ = [
    "WindowsJobError",
    "WindowsKillOnCloseJob",
    "create_kill_on_close_job",
]
