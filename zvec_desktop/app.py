"""Command-line entry point for the pure-Python desktop application."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .single_instance import (
    EXIT_RUNNING_INSTANCE_ARGUMENT,
    SingleInstanceCoordinator,
    SingleInstanceError,
    TkAfterRoot,
    TkSingleInstancePump,
)


@dataclass(frozen=True, slots=True)
class DesktopLaunchOptions:
    config_path: Path | None = None
    manifest_path: Path | None = None
    image_folder: Path | None = None
    page_size: int = 15
    exit_running_instance: bool = False


class _DesktopRoot(TkAfterRoot, Protocol):
    """Tk operations required by the single-instance activation fallback."""

    def deiconify(self) -> None: ...

    def lift(self) -> None: ...

    def focus_force(self) -> None: ...


class _DesktopWindow(Protocol):
    @property
    def root(self) -> _DesktopRoot: ...

    def run(self) -> None: ...

    def close(self) -> None: ...


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zvec-desktop",
        description="Zvec pure-Python image library desktop client.",
    )
    parser.add_argument("--config", type=Path, help="Override config.json path.")
    parser.add_argument(
        "--results-manifest",
        type=Path,
        help="Open one existing results.json file.",
    )
    parser.add_argument(
        "--folder",
        type=Path,
        help="Open an image folder without changing the Zvec configuration.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=15,
        help="Images per gallery page (default: 15).",
    )
    parser.add_argument(
        EXIT_RUNNING_INSTANCE_ARGUMENT,
        action="store_true",
        help="Ask the running desktop instance to exit, then stop.",
    )
    return parser


def parse_options(argv: Sequence[str] | None = None) -> DesktopLaunchOptions:
    args = build_parser().parse_args(argv)
    if args.page_size < 1 or args.page_size > 100:
        raise ValueError("--page-size must be between 1 and 100.")
    return DesktopLaunchOptions(
        config_path=args.config,
        manifest_path=args.results_manifest,
        image_folder=args.folder,
        page_size=args.page_size,
        exit_running_instance=args.exit_running_instance,
    )


def _create_window(options: DesktopLaunchOptions) -> _DesktopWindow:
    """Load GUI-only dependencies after this process becomes the primary."""

    from .tray import TrayIconService
    from .ui import ZvecDesktopWindow

    return ZvecDesktopWindow(options, tray_service=TrayIconService())


def _activate_window(window: _DesktopWindow) -> None:
    """Restore the primary window on Tk's thread.

    ``show_window`` is the preferred public UI boundary.  The root fallback
    keeps this entry point compatible while the pure-Python window is being
    assembled from independently tested panels.
    """

    for method_name in ("show_window", "restore_window", "activate_window"):
        callback = getattr(window, method_name, None)
        if callable(callback):
            callback()
            return
    window.root.deiconify()
    window.root.lift()
    window.root.focus_force()


def _report_coordination_warning(error: SingleInstanceError) -> None:
    print(f"Zvec Desktop instance warning: {error}", file=sys.stderr)


def _run_primary_window(
    options: DesktopLaunchOptions,
    coordinator: SingleInstanceCoordinator,
) -> int:
    window = _create_window(options)
    pump = TkSingleInstancePump(
        window.root,
        coordinator,
        on_activate=lambda: _activate_window(window),
        on_exit=window.close,
        on_error=_report_coordination_warning,
    )
    pump.start()
    try:
        window.run()
    finally:
        # ``stop`` tolerates an already-destroyed Tk root and prevents a late
        # scheduled callback from retaining the window during interpreter exit.
        pump.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    coordinator: SingleInstanceCoordinator | None = None
    try:
        options = parse_options(argv)
        coordinator = SingleInstanceCoordinator()
        start_result = coordinator.start(
            request_existing_exit=options.exit_running_instance
        )
        if not start_result.should_run_ui:
            if start_result.error is not None:
                _report_coordination_warning(start_result.error)
            return start_result.exit_code
        return _run_primary_window(options, coordinator)
    except Exception as exc:
        # The console-script boundary converts startup failures into a stable
        # exit code instead of leaving a frozen executable with a traceback.
        print(f"Unable to start Zvec Desktop: {exc}", file=sys.stderr)
        return 1
    finally:
        if coordinator is not None:
            try:
                coordinator.close()
            except Exception as exc:
                # Native handles are also process-owned, so a cleanup warning
                # must not replace the meaningful launch/activation exit code.
                print(
                    f"Unable to release Zvec Desktop instance resources: {exc}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    raise SystemExit(main())
