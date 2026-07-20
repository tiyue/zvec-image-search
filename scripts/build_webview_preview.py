"""Build and verify the isolated Windows x64 pywebview Preview payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

if __package__:
    from scripts.webview_preview_packaging import (
        PRODUCT_DIRECTORY,
        BuildPlan,
        WebviewPreviewPackagingError,
        create_build_plan,
        validate_build_runtime,
        validate_frontend_output,
        validate_static_inputs,
        verify_payload_manifest,
        write_payload_manifest,
        write_portable_archive,
    )
else:
    from webview_preview_packaging import (
        PRODUCT_DIRECTORY,
        BuildPlan,
        WebviewPreviewPackagingError,
        create_build_plan,
        validate_build_runtime,
        validate_frontend_output,
        validate_static_inputs,
        verify_payload_manifest,
        write_payload_manifest,
        write_portable_archive,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build the independent CPython 3.12/PyInstaller Windows x64 "
            "Zvec Webview Preview after a clean, tested Vite frontend build."
        )
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Independent output directory; defaults under dist/webview-preview.",
    )
    parser.add_argument(
        "--work-directory",
        type=Path,
        help="Parent directory for the isolated win-x64 PyInstaller work tree.",
    )
    parser.add_argument(
        "--makensis",
        type=Path,
        help="Optional path to makensis.exe for the dedicated WebView installer.",
    )
    parser.add_argument(
        "--use-existing-frontend",
        action="store_true",
        help=(
            "Validate and freeze the existing frontend_dist without running npm or "
            "rewriting its files."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate source inputs and print the build plan without building.",
    )
    parser.add_argument(
        "--verify-only",
        type=Path,
        metavar="PAYLOAD_DIRECTORY",
        help="Verify an existing Preview payload and its SHA-256 manifest.",
    )
    return parser


def _safe_remove_tree(path: Path, *, expected_name: str) -> None:
    resolved = path.resolve()
    if resolved.name.casefold() != expected_name.casefold():
        raise WebviewPreviewPackagingError(
            f"Refusing to clean an unexpected Preview directory: {resolved}"
        )
    if resolved.parent == resolved or len(resolved.parts) < 3:
        raise WebviewPreviewPackagingError(
            f"Refusing to clean an unsafe Preview path: {resolved}"
        )
    if resolved.exists():
        shutil.rmtree(resolved)


def _run(command: Sequence[str], *, cwd: Path) -> None:
    completed = subprocess.run(list(command), cwd=cwd, check=False)
    if completed.returncode != 0:
        raise WebviewPreviewPackagingError(
            f"Preview build failed with exit code {completed.returncode}: "
            + " ".join(command)
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _build_frontend(plan: BuildPlan) -> dict[str, object]:
    """Install the locked Node graph, verify it, and emit one Vite payload."""

    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if npm is None:
        raise WebviewPreviewPackagingError(
            "npm is required to build the Webview Preview frontend."
        )
    for arguments in (
        ("ci",),
        ("run", "typecheck"),
        ("test",),
        ("run", "build"),
    ):
        _run((npm, *arguments), cwd=plan.frontend_directory)
    return validate_frontend_output(plan)


def _prepare_frontend(
    plan: BuildPlan,
    *,
    rebuild: bool,
) -> dict[str, object]:
    """Either rebuild the locked Vite graph or validate an approved build."""

    return _build_frontend(plan) if rebuild else validate_frontend_output(plan)


def _run_frozen_self_test(plan: BuildPlan) -> dict[str, object]:
    """Prove the frozen pythonnet/WebView2 imports without opening the UI."""

    output_path = plan.work_directory / "frozen-self-test.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    executable = plan.payload_directory / "Zvec.WebviewPreview.exe"
    try:
        completed = subprocess.run(
            [
                str(executable),
                "--zvec-packaging-self-test",
                str(output_path),
            ],
            cwd=plan.payload_directory,
            check=False,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            details = ""
            if output_path.is_file():
                details = output_path.read_text(encoding="utf-8", errors="replace")
            raise WebviewPreviewPackagingError(
                "Frozen WebView2 self-test failed with exit code "
                f"{completed.returncode}: {details.strip()}"
            )
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WebviewPreviewPackagingError(
                f"Frozen WebView2 self-test did not produce valid JSON: {exc}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise WebviewPreviewPackagingError(
                f"Frozen WebView2 self-test returned an invalid result: {payload!r}"
            )
        frontend = payload.get("frontend")
        if not isinstance(frontend, dict):
            raise WebviewPreviewPackagingError(
                "Frozen WebView2 self-test did not validate the Vite frontend."
            )
        frontend_files = frontend.get("files")
        if (
            frontend.get("manifest") != ".vite/manifest.json"
            or frontend.get("entry_key") != "index.html"
            or not isinstance(frontend_files, list)
            or len(frontend_files) < 4
            or not all(isinstance(item, str) for item in frontend_files)
            or any("node_modules" in item.casefold() for item in frontend_files)
            or payload.get("asset_count") != len(frontend_files) + 5
        ):
            raise WebviewPreviewPackagingError(
                "Frozen WebView2 self-test returned an invalid Vite asset closure."
            )
        modules = payload.get("modules")
        required_modules = {
            "PIL.Image",
            "clr",
            "image_vector_service.active_learning",
            "image_vector_service.active_learning_review_store",
            "image_vector_service.activity_store",
            "image_vector_service.cluster_operation_store",
            "image_vector_service.data_migration",
            "image_vector_service.migration_recovery",
            "image_vector_service.folder_deletion",
            "image_vector_service.image_clustering",
            "image_vector_service.large_cluster_adapter",
            "image_vector_service.large_image_clustering",
            "image_vector_service.library_browser",
            "image_vector_service.learning_ranker",
            "image_vector_service.search_features",
            "image_vector_service.search_learning_evaluator",
            "image_vector_service.search_learning_config",
            "image_vector_service.search_learning_runtime",
            "image_vector_service.search_learning_service",
            "image_vector_service.search_learning_store",
            "webview",
            "webview.platforms.edgechromium",
            "webview.platforms.winforms",
            "zvec",
            "zvec_webview.app",
            "zvec_webview.facade",
            "zvec_webview.frontend_assets",
            "zvec_webview.native_bridge",
            "zvec_webview.runtime",
            "zvec_webview.server",
        }
        if not isinstance(modules, list) or not required_modules.issubset(modules):
            raise WebviewPreviewPackagingError(
                "Frozen WebView2 self-test did not import its full runtime closure."
            )
        persistence = payload.get("persistence")
        if (
            not isinstance(persistence, dict)
            or persistence.get("cluster_store_schema") != 1
            or persistence.get("cluster_store_api_requests") != 0
            or persistence.get("active_learning_recovered") != 0
        ):
            raise WebviewPreviewPackagingError(
                "Frozen WebView2 self-test did not validate local learning storage."
            )
        sqlite = payload.get("sqlite")
        if (
            not isinstance(sqlite, dict)
            or sqlite.get("module") != "sqlite3"
            or sqlite.get("journal_mode") != "wal"
            or sqlite.get("row_count") != 1
        ):
            raise WebviewPreviewPackagingError(
                "Frozen Preview self-test did not prove writable SQLite WAL support."
            )
        return payload
    finally:
        output_path.unlink(missing_ok=True)


def build(
    plan: BuildPlan,
    *,
    rebuild_frontend: bool = True,
) -> dict[str, object]:
    """Build one verified payload, portable ZIP, and optional NSIS installer."""

    validate_build_runtime()
    frontend = _prepare_frontend(plan, rebuild=rebuild_frontend)
    plan.output_root.mkdir(parents=True, exist_ok=True)
    plan.work_directory.parent.mkdir(parents=True, exist_ok=True)
    # Never leave a previous ZIP looking current after a failed clean rebuild.
    plan.portable_output.unlink(missing_ok=True)
    plan.installer_output.unlink(missing_ok=True)
    _safe_remove_tree(plan.payload_directory, expected_name=PRODUCT_DIRECTORY)
    _safe_remove_tree(plan.work_directory, expected_name="win-x64")

    _run(plan.pyinstaller_command, cwd=plan.repository_root)
    frozen_self_test = _run_frozen_self_test(plan)
    manifest_path = write_payload_manifest(
        plan.payload_directory,
        version=plan.version,
    )
    manifest = verify_payload_manifest(plan.payload_directory)
    portable = write_portable_archive(plan.payload_directory, plan.portable_output)
    installer: dict[str, object] | None = None
    if plan.makensis_command is not None:
        _run(plan.makensis_command, cwd=plan.repository_root)
        if (
            not plan.installer_output.is_file()
            or plan.installer_output.stat().st_size <= 0
        ):
            raise WebviewPreviewPackagingError(
                f"NSIS completed without producing {plan.installer_output}"
            )
        installer = {
            "path": str(plan.installer_output),
            "size": plan.installer_output.stat().st_size,
            "sha256": _sha256(plan.installer_output),
        }
    return {
        "status": "ok",
        "product": "Zvec Webview Preview",
        "payload_directory": str(plan.payload_directory),
        "manifest": str(manifest_path),
        "file_count": manifest["file_count"],
        "total_size": manifest["total_size"],
        "frozen_self_test": frozen_self_test,
        "frontend": frontend,
        "portable": portable,
        "installer": installer,
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
                        "product": "Zvec Webview Preview",
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
        print(
            json.dumps(
                build(
                    plan,
                    rebuild_frontend=not args.use_existing_frontend,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (OSError, WebviewPreviewPackagingError, subprocess.SubprocessError) as exc:
        print(f"Webview Preview build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
