"""Build and verify the PowerShell-free Windows x64 Python preview."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__:
    from scripts.python_preview_packaging import (
        BuildPlan,
        PreviewPackagingError,
        create_build_plan,
        validate_build_runtime,
        validate_static_inputs,
        verify_payload_manifest,
        write_payload_manifest,
        write_portable_archive,
    )
else:
    from python_preview_packaging import (
        BuildPlan,
        PreviewPackagingError,
        create_build_plan,
        validate_build_runtime,
        validate_static_inputs,
        verify_payload_manifest,
        write_payload_manifest,
        write_portable_archive,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the CPython 3.12/PyInstaller Windows x64 Zvec Python preview. "
            "The script never installs or downloads dependencies."
        )
    )
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--work-directory",
        type=Path,
        help="Parent directory for the isolated win-x64 PyInstaller work tree.",
    )
    parser.add_argument(
        "--makensis",
        type=Path,
        help="Optional path to makensis.exe for the independent preview installer.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print the build plan without changing files.",
    )
    parser.add_argument(
        "--verify-only",
        type=Path,
        metavar="PAYLOAD_DIRECTORY",
        help="Verify one existing payload and its SHA-256 manifest.",
    )
    return parser


def _safe_remove_tree(path: Path, *, expected_name: str) -> None:
    resolved = path.resolve()
    if resolved.name.casefold() != expected_name.casefold():
        raise PreviewPackagingError(
            f"Refusing to clean an unexpected build directory: {resolved}"
        )
    if resolved.parent == resolved or len(resolved.parts) < 3:
        raise PreviewPackagingError(f"Refusing to clean unsafe path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _run(command: Sequence[str], *, cwd: Path) -> None:
    completed = subprocess.run(list(command), cwd=cwd, check=False)
    if completed.returncode != 0:
        raise PreviewPackagingError(
            f"Build command failed with exit code {completed.returncode}: "
            + " ".join(command)
        )


def _build(plan: BuildPlan) -> dict[str, object]:
    validate_build_runtime()
    plan.output_root.mkdir(parents=True, exist_ok=True)
    plan.work_directory.parent.mkdir(parents=True, exist_ok=True)
    _safe_remove_tree(plan.payload_directory, expected_name="Zvec-Python-Preview")
    _safe_remove_tree(plan.work_directory, expected_name="win-x64")

    _run(plan.pyinstaller_command, cwd=plan.repository_root)
    pyinstaller_version = importlib.metadata.version("PyInstaller")
    manifest_path = write_payload_manifest(
        plan.payload_directory,
        version=plan.version,
        pyinstaller_version=pyinstaller_version,
    )
    manifest = verify_payload_manifest(plan.payload_directory)
    portable = write_portable_archive(
        plan.payload_directory,
        plan.portable_output,
    )

    installer_built = False
    if plan.makensis_command is not None:
        plan.installer_output.unlink(missing_ok=True)
        _run(plan.makensis_command, cwd=plan.repository_root)
        if (
            not plan.installer_output.is_file()
            or plan.installer_output.stat().st_size <= 0
        ):
            raise PreviewPackagingError(
                f"NSIS completed without producing {plan.installer_output}"
            )
        installer_built = True

    return {
        "status": "ok",
        "payload_directory": str(plan.payload_directory),
        "manifest": str(manifest_path),
        "file_count": manifest["file_count"],
        "total_size": manifest["total_size"],
        "portable": portable,
        "installer": str(plan.installer_output) if installer_built else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.verify_only is not None:
            manifest = verify_payload_manifest(args.verify_only)
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "payload_directory": str(args.verify_only.resolve()),
                        "file_count": manifest["file_count"],
                        "total_size": manifest["total_size"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        plan = create_build_plan(
            output_root=args.output_root,
            work_directory=args.work_directory,
            makensis=args.makensis,
        )
        validate_static_inputs(plan)
        if args.dry_run:
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            return 0
        print(json.dumps(_build(plan), ensure_ascii=False, indent=2))
        return 0
    except (OSError, PreviewPackagingError, subprocess.SubprocessError) as exc:
        print(f"Python preview build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
