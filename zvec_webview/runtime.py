"""Lifecycle coordinator for the pywebview window and local services."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from zvec_desktop.backend_host import BackendBusyError

from .facade import PreviewFacade
from .server import GatewayAddress, GatewayServer


@dataclass(frozen=True, slots=True)
class RuntimeStart:
    address: GatewayAddress
    backend_started_async: bool


class PreviewRuntime:
    """Start the UI gateway first, then initialise the backend in the background."""

    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        facade: PreviewFacade | None = None,
        gateway: GatewayServer | None = None,
    ) -> None:
        self.facade = facade or PreviewFacade(config_path)
        self.gateway = gateway or GatewayServer(self.facade)
        self._lock = threading.RLock()
        self._started = False
        self._closed = False

    def start(self) -> RuntimeStart:
        with self._lock:
            if self._closed:
                raise RuntimeError("Preview runtime has already been closed.")
            address = self.gateway.start()
            if self._started:
                return RuntimeStart(address, backend_started_async=False)
            self._started = True
        future = self.facade.start_backend_async()
        return RuntimeStart(address, backend_started_async=future is not None)

    def close(self, *, force: bool = False) -> None:
        """Stop only when jobs are idle unless the caller explicitly forces it."""

        with self._lock:
            if self._closed:
                return
        try:
            self.facade.close(force=force)
        except BackendBusyError:
            # Keep both the page gateway and backend alive so the user can return
            # to the task page instead of losing control of accepted work.
            raise
        self.gateway.stop()
        with self._lock:
            self._closed = True


__all__ = ["PreviewRuntime", "RuntimeStart"]
