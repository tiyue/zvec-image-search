"""Single frozen dispatcher shared by all Python preview executables.

PyInstaller builds one dependency graph and places three small bootloaders next
to the same ``_internal`` directory.  Dispatching by executable name keeps the
runtime closure shared instead of publishing three duplicate applications.
"""

from __future__ import annotations

import multiprocessing
import sys
from collections.abc import Sequence
from pathlib import Path


class FrozenEntryError(RuntimeError):
    """Raised when the shared frozen launcher has an unknown executable name."""


def _entry_name(executable: str | None = None) -> str:
    selected = executable or sys.executable
    return Path(selected).stem.casefold()


def _compatibility_module_args(arguments: list[str]) -> tuple[str, list[str]] | None:
    """Recognize the old ``python -m module`` subprocess contract.

    The compatibility path lets an intermediate Python desktop build start the
    backend before ``BackendHost`` is switched to the sibling backend executable.
    It can be removed after that migration is complete.
    """

    if len(arguments) < 2 or arguments[0] != "-m":
        return None
    module_name = arguments[1].casefold()
    if module_name not in {"image_service", "zvec_launcher"}:
        return None
    return module_name, arguments[2:]


def main(
    argv: Sequence[str] | None = None,
    *,
    executable: str | None = None,
) -> int:
    """Dispatch to the desktop, CLI, or persistent-backend Python entry point."""

    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)

    compatibility = _compatibility_module_args(arguments)
    if compatibility is not None:
        module_name, module_arguments = compatibility
        if module_name == "image_service":
            from image_service import main as backend_main

            return backend_main(module_arguments)
        from zvec_launcher import main as launcher_main

        return launcher_main(module_arguments)

    entry_name = _entry_name(executable)
    if entry_name == "zvec.desktop":
        from zvec_desktop.app import main as desktop_main

        return desktop_main(arguments)
    if entry_name == "zvec":
        from zvec_launcher import main as launcher_main

        return launcher_main(arguments)
    if entry_name == "zvec-backend":
        from image_service import main as backend_main

        return backend_main(arguments or ["serve"])
    raise FrozenEntryError(f"Unknown frozen Zvec entry point: {entry_name}")


if __name__ == "__main__":
    raise SystemExit(main())
