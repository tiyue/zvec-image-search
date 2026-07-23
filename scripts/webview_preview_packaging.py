"""Build planning and payload contracts for the Windows x64 Webview Preview."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import stat
import struct
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from zvec_webview.frontend_assets import (
    VITE_MANIFEST,
    FrontendAssetError,
    validate_frontend_build,
)

PRODUCT_NAME: Final = "Zvec Webview Preview"
PRODUCT_DIRECTORY: Final = "Zvec-Webview-Preview"
MANIFEST_FILE: Final = "webview-preview-payload-manifest.json"
MANIFEST_SCHEMA_VERSION: Final = 1
TARGET_RUNTIME: Final = "win-x64"
TARGET_PYTHON: Final = "CPython 3.12"
PYINSTALLER_VERSION: Final = "6.16.0"

ENTRY_POINTS: Final = (
    "Zvec.WebviewPreview.exe",
    "zvec.exe",
    "zvec-backend.exe",
)
LOCKED_DISTRIBUTIONS: Final = {
    "PyInstaller": PYINSTALLER_VERSION,
    "bottle": "0.13.4",
    "cffi": "2.1.0",
    "clr_loader": "0.3.1",
    "proxy_tools": "0.1.0",
    "pycparser": "3.0",
    "pythonnet": "3.1.0",
    "pywebview": "6.2.1",
    "typing_extensions": "4.16.0",
}
EXCLUDED_PACKAGES: Final = frozenset(
    {
        "cefpython3",
        "openai",
        "pyqt5",
        "pyqt6",
        "pyside2",
        "pyside6",
        "pystray",
        "tkinter",
        "torch",
        "transformers",
    }
)

_PROJECT_VERSION = re.compile(
    r"(?m)^\s*version\s*=\s*[\"'](?P<version>[^\"']+)[\"']\s*$"
)
_WINDOWS_FILE_VERSION_PART = re.compile(r"\d+")
_FORBIDDEN_SOURCE_SUFFIXES = frozenset(
    {".cs", ".csproj", ".ps1", ".psd1", ".psm1", ".sln", ".xaml"}
)
_FORBIDDEN_RUNTIME_NAMES = frozenset(
    {
        "docker.exe",
        "dockerfile",
        "dotnet.exe",
        "powershell.exe",
        "pwsh.exe",
        "webbrowserinterop.x86.dll",
        "zvec.desktop.exe",
    }
)
_FORBIDDEN_CONTAINER_PREFIXES = ("compose.", "docker-compose.", "dockerfile.")
_REQUIRED_RELATIVE_SUFFIXES = (
    "zvec_webview/frontend_dist/index.html",
    "zvec_webview/frontend_dist/.vite/manifest.json",
    "webview/js/api.js",
    "webview/js/finish.js",
    "webview/lib/runtimes/win-x64/native/webview2loader.dll",
)
_REQUIRED_NAMED_FILES = (
    "python312.dll",
    "Python.Runtime.dll",
    "netstandard.dll",
    "System.Runtime.dll",
    "Microsoft.Web.WebView2.Core.dll",
    "Microsoft.Web.WebView2.WinForms.dll",
    "WebBrowserInterop.x64.dll",
    "model-catalog.default.json",
    "Zvec.AppIcon.ico",
)
# Minimum .NET Standard 2.0 facade assemblies that must co-locate with
# Python.Runtime.dll so the .NET Framework CLR can resolve its type
# dependencies when loaded via clr_loader's custom AppDomain.
_REQUIRED_PYTHONNET_RUNTIME_FILES = (
    "pythonnet/runtime/Python.Runtime.dll",
    "pythonnet/runtime/netstandard.dll",
    "pythonnet/runtime/System.Runtime.dll",
)
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class WebviewPreviewPackagingError(RuntimeError):
    """Raised when a Preview build violates its Windows x64 contract."""


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """Side-effect-free description of one isolated Preview build."""

    repository_root: Path
    version: str
    spec_path: Path
    icon_path: Path
    nsis_path: Path
    requirements_path: Path
    frontend_directory: Path
    frontend_output: Path
    frontend_manifest: Path
    output_root: Path
    payload_directory: Path
    work_directory: Path
    portable_output: Path
    installer_output: Path
    pyinstaller_command: tuple[str, ...]
    makensis_command: tuple[str, ...] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "product": PRODUCT_NAME,
            "version": self.version,
            "target_runtime": TARGET_RUNTIME,
            "target_python": TARGET_PYTHON,
            "repository_root": str(self.repository_root),
            "spec_path": str(self.spec_path),
            "icon_path": str(self.icon_path),
            "nsis_path": str(self.nsis_path),
            "requirements_path": str(self.requirements_path),
            "frontend_directory": str(self.frontend_directory),
            "frontend_output": str(self.frontend_output),
            "frontend_manifest": str(self.frontend_manifest),
            "output_root": str(self.output_root),
            "payload_directory": str(self.payload_directory),
            "work_directory": str(self.work_directory),
            "portable_output": str(self.portable_output),
            "installer_output": str(self.installer_output),
            "entry_points": list(ENTRY_POINTS),
            "locked_distributions": dict(sorted(LOCKED_DISTRIBUTIONS.items())),
            "excluded_packages": sorted(EXCLUDED_PACKAGES),
            "host": {
                "implementation": platform.python_implementation(),
                "python": platform.python_version(),
                "machine": platform.machine(),
                "platform": sys.platform,
            },
            "command": list(self.pyinstaller_command),
            "makensis_command": (
                list(self.makensis_command)
                if self.makensis_command is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class PayloadInspection:
    """Deterministic result of inspecting one frozen onedir payload."""

    files: tuple[Path, ...]
    errors: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def require_valid(self) -> None:
        if self.errors:
            raise WebviewPreviewPackagingError("\n".join(self.errors))


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_project_version(root: Path) -> str:
    project_path = root / "pyproject.toml"
    try:
        content = project_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WebviewPreviewPackagingError(
            f"Unable to read {project_path}: {exc}"
        ) from exc
    match = _PROJECT_VERSION.search(content)
    if match is None:
        raise WebviewPreviewPackagingError("pyproject.toml has no project.version")
    version = match.group("version").strip()
    if not version or any(character in version for character in "\r\n\0"):
        raise WebviewPreviewPackagingError("pyproject.toml has an unsafe version")
    return version


def windows_file_version(version: str) -> str:
    """Convert a project version to the four numeric parts NSIS requires."""

    numbers = [int(value) for value in _WINDOWS_FILE_VERSION_PART.findall(version)]
    if len(numbers) < 3:
        raise WebviewPreviewPackagingError(
            f"Version must contain at least three numeric parts: {version}"
        )
    selected = (numbers + [0, 0, 0, 0])[:4]
    if any(value > 65535 for value in selected):
        raise WebviewPreviewPackagingError(
            "Windows file-version parts must not exceed 65535"
        )
    return ".".join(str(value) for value in selected)


def create_build_plan(
    *,
    root: Path | None = None,
    output_root: Path | None = None,
    work_directory: Path | None = None,
    makensis: Path | None = None,
) -> BuildPlan:
    resolved_root = (root or repository_root()).resolve()
    version = read_project_version(resolved_root)
    resolved_output = (
        output_root.resolve()
        if output_root is not None
        else resolved_root / "dist" / "webview-preview" / version / TARGET_RUNTIME
    )
    work_parent = (
        work_directory.resolve()
        if work_directory is not None
        else resolved_root / "build" / "webview-preview"
    )
    resolved_work = work_parent / TARGET_RUNTIME
    payload_directory = resolved_output / PRODUCT_DIRECTORY
    portable_output = resolved_output / (
        f"Zvec-Webview-Preview-{version}-{TARGET_RUNTIME}-portable.zip"
    )
    installer_output = resolved_output / (
        f"Zvec-Webview-Preview-{version}-{TARGET_RUNTIME}-unsigned-setup.exe"
    )
    spec_path = (
        resolved_root / "release" / "webview_preview" / "zvec_webview_preview.spec"
    )
    command = (
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(resolved_output),
        "--workpath",
        str(resolved_work),
        str(spec_path),
    )
    nsis_path = resolved_root / "installer" / "Zvec.WebviewPreview.nsi"
    makensis_command: tuple[str, ...] | None = None
    if makensis is not None:
        makensis_command = (
            str(makensis.resolve()),
            f"/DVERSION={version}",
            f"/DFILE_VERSION={windows_file_version(version)}",
            f"/DSOURCE_DIR={payload_directory}",
            f"/DOUTPUT_FILE={installer_output}",
            f"/DRID={TARGET_RUNTIME}",
            "/DSIGNING_STATUS=unsigned",
            str(nsis_path),
        )
    return BuildPlan(
        repository_root=resolved_root,
        version=version,
        spec_path=spec_path,
        icon_path=resolved_root / "assets" / "Zvec.AppIcon.ico",
        nsis_path=nsis_path,
        requirements_path=resolved_root / "requirements-webview-preview-lock.txt",
        frontend_directory=resolved_root / "frontend",
        frontend_output=resolved_root / "zvec_webview" / "frontend_dist",
        frontend_manifest=(
            resolved_root / "zvec_webview" / "frontend_dist" / Path(VITE_MANIFEST)
        ),
        output_root=resolved_output,
        payload_directory=payload_directory,
        work_directory=resolved_work,
        portable_output=portable_output,
        installer_output=installer_output,
        pyinstaller_command=command,
        makensis_command=makensis_command,
    )


def validate_static_inputs(plan: BuildPlan) -> None:
    required_files = (
        plan.repository_root / "pyproject.toml",
        plan.repository_root / "model-catalog.default.json",
        plan.repository_root / "image_vector_service" / "active_learning.py",
        plan.repository_root
        / "image_vector_service"
        / "active_learning_review_store.py",
        plan.repository_root / "image_vector_service" / "activity_store.py",
        plan.repository_root / "image_vector_service" / "cluster_operation_store.py",
        plan.repository_root / "image_vector_service" / "folder_deletion.py",
        plan.repository_root / "image_vector_service" / "image_clustering.py",
        plan.repository_root / "image_vector_service" / "large_cluster_adapter.py",
        plan.repository_root / "image_vector_service" / "large_image_clustering.py",
        plan.repository_root / "image_vector_service" / "library_browser.py",
        plan.repository_root / "image_vector_service" / "learning_ranker.py",
        plan.repository_root / "image_vector_service" / "search_features.py",
        plan.repository_root / "image_vector_service" / "search_learning_config.py",
        plan.repository_root / "image_vector_service" / "search_learning_evaluator.py",
        plan.repository_root / "image_vector_service" / "search_learning_runtime.py",
        plan.repository_root / "image_vector_service" / "search_learning_service.py",
        plan.repository_root / "image_vector_service" / "search_learning_store.py",
        plan.repository_root / "zvec_lan" / "__init__.py",
        plan.repository_root / "zvec_lan" / "discovery.py",
        plan.repository_root / "zvec_lan" / "http_server.py",
        plan.repository_root / "zvec_lan" / "models.py",
        plan.repository_root / "zvec_lan" / "pairing.py",
        plan.repository_root / "zvec_lan" / "service.py",
        plan.repository_root / "zvec_lan" / "uploads.py",
        plan.repository_root / "zvec_webview" / "__init__.py",
        plan.repository_root / "zvec_webview" / "app.py",
        plan.repository_root / "zvec_webview" / "facade.py",
        plan.repository_root / "zvec_webview" / "image_registry.py",
        plan.repository_root / "zvec_webview" / "lan_access.py",
        plan.repository_root / "zvec_webview" / "lan_settings.py",
        plan.repository_root / "zvec_webview" / "native_bridge.py",
        plan.repository_root / "zvec_webview" / "runtime.py",
        plan.repository_root / "zvec_webview" / "server.py",
        plan.repository_root / "zvec_webview" / "frontend_assets.py",
        plan.frontend_directory / "package.json",
        plan.frontend_directory / "package-lock.json",
        plan.frontend_directory / "vite.config.ts",
        plan.frontend_directory / "index.html",
        plan.frontend_directory / "src" / "main.ts",
        plan.frontend_directory / "src" / "App.vue",
        plan.frontend_directory
        / "src"
        / "features"
        / "activity"
        / "ActivityLogTable.vue",
        plan.frontend_directory
        / "src"
        / "features"
        / "activity"
        / "JobHistoryTable.vue",
        plan.frontend_directory
        / "src"
        / "features"
        / "activity"
        / "useActivityCenter.ts",
        plan.frontend_directory / "src" / "features" / "organize" / "OrganizePage.vue",
        plan.frontend_directory
        / "src"
        / "features"
        / "organize"
        / "galleryCapacity.ts",
        plan.frontend_directory / "src" / "features" / "tasks" / "TasksPage.vue",
        plan.repository_root / "release" / "webview_preview" / "frozen_entry.py",
        plan.repository_root / "release" / "webview_preview" / "runtime_hook.py",
        plan.repository_root
        / "release"
        / "webview_preview"
        / "hooks"
        / "hook-webview.py",
        plan.repository_root / "release" / "webview_preview" / "hooks" / "hook-clr.py",
        plan.repository_root / "release" / "python_preview" / "hooks" / "hook-zvec.py",
        plan.spec_path,
        plan.icon_path,
        plan.nsis_path,
        plan.requirements_path,
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    if plan.makensis_command is not None:
        compiler = Path(plan.makensis_command[0])
        if not compiler.is_file():
            missing.append(str(compiler))
    if missing:
        raise WebviewPreviewPackagingError(
            "Webview Preview build inputs are missing:\n" + "\n".join(missing)
        )


def validate_frontend_output(plan: BuildPlan) -> dict[str, object]:
    """Require a complete, local-only Vite build before PyInstaller runs."""

    try:
        build = validate_frontend_build(plan.frontend_output)
    except FrontendAssetError as exc:
        raise WebviewPreviewPackagingError(
            f"Webview Preview frontend build is invalid: {exc}"
        ) from exc
    if build.manifest_path != plan.frontend_manifest.resolve():
        raise WebviewPreviewPackagingError(
            "Webview Preview frontend manifest was loaded from an unexpected path."
        )
    return build.to_dict()


def target_host_errors(
    *,
    os_name: str,
    platform_name: str,
    implementation: str,
    python_version: tuple[int, int],
    machine: str,
    pointer_size: int,
) -> tuple[str, ...]:
    """Return portable host-contract failures for deterministic unit tests."""

    errors: list[str] = []
    if os_name != "nt" or platform_name != "win32":
        errors.append("Webview Preview builds are supported on Windows only.")
    if implementation != "CPython":
        errors.append("Webview Preview builds require CPython.")
    if python_version != (3, 12):
        errors.append(
            "Webview Preview builds are pinned to CPython 3.12; "
            f"received {python_version[0]}.{python_version[1]}."
        )
    if pointer_size != 8 or machine.casefold() not in {"amd64", "x86_64"}:
        errors.append("Webview Preview builds require a native Windows x64 process.")
    return tuple(errors)


def validate_build_runtime() -> None:
    errors = list(
        target_host_errors(
            os_name=os.name,
            platform_name=sys.platform,
            implementation=platform.python_implementation(),
            python_version=sys.version_info[:2],
            machine=platform.machine(),
            pointer_size=struct.calcsize("P"),
        )
    )
    for distribution, expected in LOCKED_DISTRIBUTIONS.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            errors.append(
                f"{distribution} is missing; install "
                "requirements-webview-preview-lock.txt in an isolated "
                "build environment."
            )
        else:
            if actual != expected:
                errors.append(f"{distribution} {expected} is required; found {actual}.")
    if importlib.util.find_spec("webview") is None:
        errors.append("The pywebview import package 'webview' is unavailable.")
    if errors:
        raise WebviewPreviewPackagingError("\n".join(dict.fromkeys(errors)))


def inspect_payload(payload_directory: Path) -> PayloadInspection:
    supplied_root = payload_directory.expanduser().absolute()
    root = supplied_root.resolve()
    errors: list[str] = []
    if _is_link_or_reparse_point(supplied_root):
        errors.append(
            f"Preview payload cannot be a link or reparse point: {supplied_root}"
        )
    if not root.is_dir():
        errors.append(f"Preview payload is missing: {root}")
        return PayloadInspection((), tuple(errors))

    discovered = tuple(root.rglob("*"))
    for path in discovered:
        if _is_link_or_reparse_point(path):
            errors.append(
                "Preview payload contains a link or reparse point: "
                f"{path.relative_to(root).as_posix()}"
            )
    files = tuple(
        sorted(
            (
                path
                for path in discovered
                if path.is_file() and not _is_link_or_reparse_point(path)
            ),
            key=lambda path: path.relative_to(root).as_posix().casefold(),
        )
    )
    folded_names = {path.name.casefold() for path in files}
    folded_relative = {
        path.relative_to(root).as_posix().casefold(): path for path in files
    }

    for entry_point in ENTRY_POINTS:
        executable = root / entry_point
        if not executable.is_file() or executable.is_symlink():
            errors.append(f"Required executable is missing: {entry_point}")
        elif executable.stat().st_size <= 0:
            errors.append(f"Required executable is empty: {entry_point}")

    for path in files:
        relative = path.relative_to(root)
        folded_parts = tuple(part.casefold() for part in relative.parts)
        folded_name = path.name.casefold()
        folded_path = relative.as_posix().casefold()
        if path.suffix.casefold() in _FORBIDDEN_SOURCE_SUFFIXES:
            errors.append(
                f"Forbidden source/runtime file is packaged: {relative.as_posix()}"
            )
        if folded_name in _FORBIDDEN_RUNTIME_NAMES or folded_name.startswith(
            _FORBIDDEN_CONTAINER_PREFIXES
        ):
            errors.append(
                f"Forbidden external runtime file is packaged: {relative.as_posix()}"
            )
        if "webview/lib/runtimes/win-arm64/" in folded_path or (
            "webview/lib/runtimes/win-x86/" in folded_path
        ):
            errors.append(
                f"Non-x64 WebView2 runtime is packaged: {relative.as_posix()}"
            )
        if folded_name in {"_tkinter.pyd", "tcl86t.dll", "tk86t.dll"}:
            errors.append(f"Legacy Tk runtime is packaged: {relative.as_posix()}")
        if "node_modules" in folded_parts:
            errors.append(f"node_modules must not be packaged: {relative.as_posix()}")
        if "zvec_webview/assets/" in folded_path:
            errors.append(
                f"Legacy web assets must not be packaged: {relative.as_posix()}"
            )
        for package in EXCLUDED_PACKAGES:
            if any(
                part == package
                or part.startswith(f"{package}-")
                and part.endswith((".dist-info", ".data"))
                for part in folded_parts
            ):
                errors.append(
                    f"Excluded package {package!r} is packaged: {relative.as_posix()}"
                )

    for required_name in _REQUIRED_NAMED_FILES:
        if required_name.casefold() not in folded_names:
            errors.append(f"Required runtime file is missing: {required_name}")
    for required_suffix in _REQUIRED_PYTHONNET_RUNTIME_FILES:
        if not any(
            path.endswith(required_suffix.casefold()) for path in folded_relative
        ):
            errors.append(
                f"Required pythonnet runtime file is missing: {required_suffix}"
            )
    for suffix in _REQUIRED_RELATIVE_SUFFIXES:
        if not any(path.endswith(suffix.casefold()) for path in folded_relative):
            errors.append(f"Required web asset is missing: {suffix}")
    frontend_manifests = [
        path
        for relative, path in folded_relative.items()
        if relative.endswith("zvec_webview/frontend_dist/.vite/manifest.json")
    ]
    if len(frontend_manifests) == 1:
        frontend_root = frontend_manifests[0].parents[1]
        try:
            validate_frontend_build(frontend_root)
        except FrontendAssetError as exc:
            errors.append(f"Packaged Vite frontend is invalid: {exc}")
    elif len(frontend_manifests) > 1:
        errors.append("Preview payload contains more than one Vite frontend manifest.")
    if not any(
        path.name.casefold().startswith("_zvec") and path.suffix.casefold() == ".pyd"
        for path in files
    ):
        errors.append("The zvec native extension (_zvec*.pyd) is missing.")
    if not any(
        path.name.casefold().startswith("_imaging") and path.suffix.casefold() == ".pyd"
        for path in files
    ):
        errors.append("The Pillow native image extension (_imaging*.pyd) is missing.")
    if not files:
        errors.append("Preview payload contains no files.")
    return PayloadInspection(files=files, errors=tuple(dict.fromkeys(errors)))


def _is_link_or_reparse_point(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_payload_manifest(payload_directory: Path, *, version: str) -> Path:
    inspection = inspect_payload(payload_directory)
    inspection.require_valid()
    root = payload_directory.resolve()
    entries: list[dict[str, object]] = []
    total_size = 0
    for path in inspection.files:
        if path.name == MANIFEST_FILE:
            continue
        size = path.stat().st_size
        total_size += size
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": size,
                "sha256": _sha256(path),
            }
        )
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": PRODUCT_NAME,
        "version": version,
        "target_runtime": TARGET_RUNTIME,
        "python_runtime": TARGET_PYTHON,
        "pyinstaller_version": PYINSTALLER_VERSION,
        "entry_points": list(ENTRY_POINTS),
        "locked_distributions": dict(sorted(LOCKED_DISTRIBUTIONS.items())),
        "file_count": len(entries),
        "total_size": total_size,
        "files": entries,
    }
    manifest_path = root / MANIFEST_FILE
    temporary = manifest_path.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, manifest_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise WebviewPreviewPackagingError(
            f"Unable to write Preview payload manifest: {exc}"
        ) from exc
    return manifest_path


def verify_payload_manifest(payload_directory: Path) -> dict[str, Any]:
    root = payload_directory.resolve()
    manifest_path = root / MANIFEST_FILE
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise WebviewPreviewPackagingError("Preview payload manifest is too large.")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WebviewPreviewPackagingError(
            f"Unable to read Preview payload manifest: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise WebviewPreviewPackagingError(
            "Preview payload manifest must be an object."
        )
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise WebviewPreviewPackagingError("Unsupported Preview manifest schema.")
    if payload.get("product") != PRODUCT_NAME:
        raise WebviewPreviewPackagingError("Preview manifest product does not match.")
    if payload.get("target_runtime") != TARGET_RUNTIME:
        raise WebviewPreviewPackagingError("Preview manifest runtime does not match.")
    if payload.get("entry_points") != list(ENTRY_POINTS):
        raise WebviewPreviewPackagingError(
            "Preview manifest entry points do not match."
        )
    entries = payload.get("files")
    if not isinstance(entries, list):
        raise WebviewPreviewPackagingError("Preview manifest files must be a list.")

    expected_paths: set[str] = set()
    total_size = 0
    for item in entries:
        if not isinstance(item, dict):
            raise WebviewPreviewPackagingError(
                "Preview manifest file entry is invalid."
            )
        relative_value = item.get("path")
        size_value = item.get("size")
        digest_value = item.get("sha256")
        if not isinstance(relative_value, str) or not relative_value:
            raise WebviewPreviewPackagingError("Preview manifest path is invalid.")
        relative = Path(relative_value)
        if relative.is_absolute() or ".." in relative.parts:
            raise WebviewPreviewPackagingError(
                f"Preview manifest path escapes its payload: {relative_value}"
            )
        if relative_value in expected_paths:
            raise WebviewPreviewPackagingError(
                f"Preview manifest contains duplicate path: {relative_value}"
            )
        expected_paths.add(relative_value)
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise WebviewPreviewPackagingError(
                f"Preview manifest path escapes its payload: {relative_value}"
            ) from exc
        if not path.is_file() or _is_link_or_reparse_point(path):
            raise WebviewPreviewPackagingError(
                f"Preview manifest file is missing or unsafe: {relative_value}"
            )
        if not isinstance(size_value, int) or size_value < 0:
            raise WebviewPreviewPackagingError(
                f"Preview manifest size is invalid: {relative_value}"
            )
        if path.stat().st_size != size_value:
            raise WebviewPreviewPackagingError(
                f"Preview manifest size mismatch: {relative_value}"
            )
        if not isinstance(digest_value, str) or len(digest_value) != 64:
            raise WebviewPreviewPackagingError(
                f"Preview manifest SHA-256 is invalid: {relative_value}"
            )
        if _sha256(path) != digest_value.casefold():
            raise WebviewPreviewPackagingError(
                f"Preview manifest SHA-256 mismatch: {relative_value}"
            )
        total_size += size_value

    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.name != MANIFEST_FILE
        and not _is_link_or_reparse_point(path)
    }
    if actual_files != expected_paths:
        extras = sorted(actual_files - expected_paths)
        missing = sorted(expected_paths - actual_files)
        raise WebviewPreviewPackagingError(
            "Preview manifest inventory mismatch; "
            f"extra={extras[:5]}, missing={missing[:5]}"
        )
    if payload.get("file_count") != len(entries):
        raise WebviewPreviewPackagingError("Preview manifest file_count mismatch.")
    if payload.get("total_size") != total_size:
        raise WebviewPreviewPackagingError("Preview manifest total_size mismatch.")
    inspect_payload(root).require_valid()
    return payload


def write_portable_archive(
    payload_directory: Path,
    archive_path: Path,
) -> dict[str, object]:
    verify_payload_manifest(payload_directory)
    root = payload_directory.resolve()
    output = archive_path.expanduser().absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in sorted(
                (item for item in root.rglob("*") if item.is_file()),
                key=lambda item: item.relative_to(root).as_posix().casefold(),
            ):
                archive.write(path, path.relative_to(root).as_posix())
        with zipfile.ZipFile(temporary) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                raise WebviewPreviewPackagingError(
                    f"Portable Preview archive contains a corrupt member: {corrupt}"
                )
        os.replace(temporary, output)
    except (OSError, zipfile.BadZipFile) as exc:
        temporary.unlink(missing_ok=True)
        raise WebviewPreviewPackagingError(
            f"Unable to create portable Preview archive: {exc}"
        ) from exc
    return {
        "path": str(output),
        "size": output.stat().st_size,
        "sha256": _sha256(output),
    }
