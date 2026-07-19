"""Build and verify the official PowerShell-free Windows x64 desktop."""

from __future__ import annotations

import argparse
import base64
import binascii
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__:
    from scripts.python_preview_packaging import (
        ENTRY_POINTS,
        PRODUCT_DIRECTORY,
        BuildPlan,
        DesktopPackagingError,
        create_build_plan,
        validate_build_runtime,
        validate_static_inputs,
        verify_payload_manifest,
        write_payload_manifest,
        write_portable_archive,
    )
else:
    from python_preview_packaging import (
        ENTRY_POINTS,
        PRODUCT_DIRECTORY,
        BuildPlan,
        DesktopPackagingError,
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
            "Build the CPython 3.12/PyInstaller Windows x64 Zvec Desktop. "
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
        help="Optional path to makensis.exe for the official desktop installer.",
    )
    parser.add_argument(
        "--signing-status",
        choices=("unsigned", "authenticode"),
        default="unsigned",
        help=(
            "Authenticode reads the PFX and password from protected environment "
            "variables."
        ),
    )
    parser.add_argument(
        "--signtool",
        type=Path,
        help=(
            "Optional path to signtool.exe; Windows SDK locations are searched "
            "otherwise."
        ),
    )
    parser.add_argument(
        "--timestamp-url",
        default="http://timestamp.digicert.com",
        help="RFC 3161 timestamp server used only for Authenticode builds.",
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
        help="Verify an existing desktop payload and its SHA-256 manifest.",
    )
    return parser


def _safe_remove_tree(path: Path, *, expected_name: str) -> None:
    resolved = path.resolve()
    if resolved.name.casefold() != expected_name.casefold():
        raise DesktopPackagingError(
            f"Refusing to clean an unexpected build directory: {resolved}"
        )
    if resolved.parent == resolved or len(resolved.parts) < 3:
        raise DesktopPackagingError(f"Refusing to clean unsafe path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _run(command: Sequence[str], *, cwd: Path) -> None:
    completed = subprocess.run(list(command), cwd=cwd, check=False)
    if completed.returncode != 0:
        raise DesktopPackagingError(
            f"Build command failed with exit code {completed.returncode}: "
            + " ".join(command)
        )


@dataclass(frozen=True, slots=True)
class AuthenticodeContext:
    signtool: Path
    pfx_path: Path
    password: str
    timestamp_url: str


def _find_signtool(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit.expanduser().absolute())
    discovered = shutil.which("signtool.exe") or shutil.which("signtool")
    if discovered:
        candidates.append(Path(discovered))
    program_files = os.getenv("PROGRAMFILES(X86)") or os.getenv("PROGRAMFILES")
    if program_files:
        sdk_root = Path(program_files) / "Windows Kits" / "10" / "bin"
        if sdk_root.is_dir():
            candidates.extend(
                sorted(
                    sdk_root.glob("*/x64/signtool.exe"),
                    reverse=True,
                )
            )
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise DesktopPackagingError(
        "signtool.exe was not found. Install the Windows SDK or pass --signtool."
    )


@contextmanager
def _authenticode_context(
    *,
    signing_status: str,
    signtool: Path | None,
    timestamp_url: str,
) -> Iterator[AuthenticodeContext | None]:
    if signing_status == "unsigned":
        yield None
        return
    encoded = os.getenv("ZVEC_AUTHENTICODE_PFX_BASE64", "").strip()
    password = os.getenv("ZVEC_AUTHENTICODE_PFX_PASSWORD", "")
    if not encoded or not password:
        raise DesktopPackagingError(
            "Authenticode requires both ZVEC_AUTHENTICODE_PFX_BASE64 and "
            "ZVEC_AUTHENTICODE_PFX_PASSWORD."
        )
    if not timestamp_url.startswith(("http://", "https://")):
        raise DesktopPackagingError("The timestamp URL must use HTTP or HTTPS.")
    try:
        pfx_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise DesktopPackagingError(
            "ZVEC_AUTHENTICODE_PFX_BASE64 is not valid base64."
        ) from exc
    if not pfx_bytes:
        raise DesktopPackagingError("The Authenticode PFX is empty.")
    tool = _find_signtool(signtool)
    with TemporaryDirectory(prefix="zvec-signing-") as temporary:
        pfx_path = Path(temporary) / "certificate.pfx"
        pfx_path.write_bytes(pfx_bytes)
        try:
            yield AuthenticodeContext(tool, pfx_path, password, timestamp_url)
        finally:
            # TemporaryDirectory removes the secret even after a failed build.
            pfx_path.unlink(missing_ok=True)


def _sign_and_verify(path: Path, context: AuthenticodeContext) -> None:
    sign = subprocess.run(
        [
            str(context.signtool),
            "sign",
            "/fd",
            "SHA256",
            "/td",
            "SHA256",
            "/tr",
            context.timestamp_url,
            "/f",
            str(context.pfx_path),
            "/p",
            context.password,
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if sign.returncode != 0:
        raise DesktopPackagingError(
            f"Authenticode signing failed for {path.name}; signtool exit code "
            f"was {sign.returncode}."
        )
    verify = subprocess.run(
        [str(context.signtool), "verify", "/pa", "/all", str(path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if verify.returncode != 0:
        raise DesktopPackagingError(
            f"Authenticode verification failed for {path.name}; signtool exit "
            f"code was {verify.returncode}."
        )


def build(
    plan: BuildPlan,
    *,
    authenticode: AuthenticodeContext | None = None,
) -> dict[str, object]:
    """Build all unsigned desktop artifacts from one verified payload."""

    validate_build_runtime()
    plan.output_root.mkdir(parents=True, exist_ok=True)
    plan.work_directory.parent.mkdir(parents=True, exist_ok=True)
    _safe_remove_tree(plan.payload_directory, expected_name=PRODUCT_DIRECTORY)
    _safe_remove_tree(plan.work_directory, expected_name="win-x64")

    _run(plan.pyinstaller_command, cwd=plan.repository_root)
    if authenticode is not None:
        for entry_point in ENTRY_POINTS:
            _sign_and_verify(plan.payload_directory / entry_point, authenticode)
    pyinstaller_version = importlib.metadata.version("PyInstaller")
    manifest_path = write_payload_manifest(
        plan.payload_directory,
        version=plan.version,
        pyinstaller_version=pyinstaller_version,
        signing_status=plan.signing_status,
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
            raise DesktopPackagingError(
                f"NSIS completed without producing {plan.installer_output}"
            )
        installer_built = True
        if authenticode is not None:
            _sign_and_verify(plan.installer_output, authenticode)

    return {
        "status": "ok",
        "product": "Zvec Desktop",
        "signing_status": plan.signing_status,
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
                        "product": "Zvec Desktop",
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
            signing_status=args.signing_status,
        )
        validate_static_inputs(plan)
        if args.dry_run:
            print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
            return 0
        with _authenticode_context(
            signing_status=args.signing_status,
            signtool=args.signtool,
            timestamp_url=args.timestamp_url,
        ) as authenticode:
            print(
                json.dumps(
                    build(plan, authenticode=authenticode),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    except (OSError, DesktopPackagingError, subprocess.SubprocessError) as exc:
        print(f"Python desktop build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
