"""Shared frozen dispatcher for the Windows x64 pywebview Preview payload.

One PyInstaller dependency graph is collected for the graphical Preview, the
native CLI, and the persistent backend.  Dispatching by executable name keeps
the Python, Pillow, zvec, and pythonnet runtimes in one shared ``_internal``
directory instead of publishing three duplicate applications.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory


class FrozenWebviewEntryError(RuntimeError):
    """Raised when the shared launcher receives an unknown executable name."""


_PACKAGING_SELF_TEST = "--zvec-packaging-self-test"


def _entry_name(executable: str | None = None) -> str:
    return Path(executable or sys.executable).stem.casefold()


def _compatibility_module_args(arguments: list[str]) -> tuple[str, list[str]] | None:
    """Support the source-mode ``python -m`` backend/CLI subprocess contract."""

    if len(arguments) < 2 or arguments[0] != "-m":
        return None
    module_name = arguments[1].casefold()
    if module_name not in {"image_service", "zvec_launcher"}:
        return None
    return module_name, arguments[2:]


def _run_packaging_self_test(output_path: Path) -> int:
    """Load the native webview stack without opening a window or backend."""

    try:
        import clr
        import webview
        import webview.platforms.edgechromium as edgechromium
        import webview.platforms.winforms as winforms
        import zvec
        from PIL import Image

        import image_vector_service.activity_store as activity_store
        import image_vector_service.folder_deletion as folder_deletion
        import image_vector_service.library_browser as library_browser
        import zvec_webview.app as preview_app
        import zvec_webview.facade as preview_facade
        import zvec_webview.frontend_assets as frontend_assets
        import zvec_webview.native_bridge as native_bridge
        import zvec_webview.runtime as preview_runtime
        import zvec_webview.server as preview_server

        runtime_root = Path(
            getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)
        )
        frontend = frontend_assets.validate_frontend_build(
            runtime_root / "zvec_webview" / "frontend_dist"
        )
        required_assets = (
            runtime_root / "webview" / "js" / "api.js",
            runtime_root / "webview" / "js" / "finish.js",
            runtime_root
            / "webview"
            / "lib"
            / "runtimes"
            / "win-x64"
            / "native"
            / "WebView2Loader.dll",
            runtime_root / "model-catalog.default.json",
            runtime_root / "assets" / "Zvec.AppIcon.ico",
        )
        missing = [str(path) for path in required_assets if not path.is_file()]
        if missing:
            raise FrozenWebviewEntryError(
                "Frozen Preview assets are missing: " + ", ".join(missing)
            )
        with TemporaryDirectory(
            prefix="zvec-packaging-sqlite-",
            dir=output_path.expanduser().absolute().parent,
        ) as sqlite_directory:
            sqlite_path = Path(sqlite_directory) / "activity-self-test.sqlite3"
            connection = sqlite3.connect(sqlite_path)
            try:
                journal_mode = str(
                    connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                ).casefold()
                connection.execute(
                    "CREATE TABLE packaging_self_test(value INTEGER NOT NULL)"
                )
                connection.execute("INSERT INTO packaging_self_test(value) VALUES (1)")
                connection.commit()
                row_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM packaging_self_test"
                    ).fetchone()[0]
                )
            finally:
                connection.close()
            if journal_mode != "wal" or row_count != 1:
                raise FrozenWebviewEntryError(
                    "Frozen SQLite runtime did not provide writable WAL storage."
                )
        result = {
            "status": "ok",
            "runtime_root": str(runtime_root),
            "modules": sorted(
                {
                    clr.__name__,
                    edgechromium.__name__,
                    activity_store.__name__,
                    folder_deletion.__name__,
                    library_browser.__name__,
                    frontend_assets.__name__,
                    Image.__name__,
                    native_bridge.__name__,
                    preview_app.__name__,
                    preview_facade.__name__,
                    preview_runtime.__name__,
                    preview_server.__name__,
                    webview.__name__,
                    winforms.__name__,
                    zvec.__name__,
                }
            ),
            "sqlite": {
                "module": sqlite3.__name__,
                "journal_mode": journal_mode,
                "row_count": row_count,
            },
            "asset_count": len(required_assets) + len(frontend.files),
            "frontend": frontend.to_dict(),
        }
        output = output_path.expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
        return 0
    except Exception as exc:
        with suppress(OSError):
            output_path.expanduser().absolute().write_text(
                json.dumps(
                    {
                        "status": "error",
                        "error": str(exc) or exc.__class__.__name__,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        return 1


def main(
    argv: Sequence[str] | None = None,
    *,
    executable: str | None = None,
) -> int:
    """Run the Preview, CLI, or persistent backend selected by the EXE name."""

    multiprocessing.freeze_support()
    arguments = list(sys.argv[1:] if argv is None else argv)

    if arguments and arguments[0] == _PACKAGING_SELF_TEST:
        if len(arguments) != 2:
            raise FrozenWebviewEntryError(
                f"{_PACKAGING_SELF_TEST} requires one JSON output path."
            )
        return _run_packaging_self_test(Path(arguments[1]))

    compatibility = _compatibility_module_args(arguments)
    if compatibility is not None:
        module_name, module_arguments = compatibility
        if module_name == "image_service":
            from image_service import main as backend_main

            return backend_main(module_arguments)
        from zvec_launcher import main as launcher_main

        return launcher_main(module_arguments)

    entry_name = _entry_name(executable)
    if entry_name == "zvec.webviewpreview":
        from zvec_webview.app import main as webview_main

        return webview_main(arguments)
    if entry_name == "zvec":
        from zvec_launcher import main as launcher_main

        return launcher_main(arguments)
    if entry_name == "zvec-backend":
        from image_service import main as backend_main

        return backend_main(arguments or ["serve"])
    raise FrozenWebviewEntryError(
        f"Unknown frozen Zvec Webview Preview entry point: {entry_name}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
