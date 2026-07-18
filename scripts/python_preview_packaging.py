"""Build-plan and payload validation helpers for the Python desktop preview."""

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

PRODUCT_DIRECTORY: Final = "Zvec-Python-Preview"
MANIFEST_FILE: Final = "python-preview-manifest.json"
MANIFEST_SCHEMA_VERSION: Final = 1
TARGET_RUNTIME: Final = "win-x64"
TARGET_PYTHON: Final = "CPython 3.12"
PYINSTALLER_VERSION: Final = "6.16.0"

ENTRY_POINTS: Final = (
    "Zvec.Desktop.exe",
    "zvec.exe",
    "zvec-backend.exe",
)
EXCLUDED_PACKAGES: Final = frozenset({"openai", "torch", "transformers"})

_PROJECT_VERSION = re.compile(
    r"(?m)^\s*version\s*=\s*[\"'](?P<version>[^\"']+)[\"']\s*$"
)
_WINDOWS_FILE_VERSION_PART = re.compile(r"\d+")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_DOTNET_RUNTIME_FILES = frozenset(
    {
        "clrjit.dll",
        "coreclr.dll",
        "hostfxr.dll",
        "hostpolicy.dll",
        "presentationcore.dll",
        "presentationframework.dll",
        "system.xaml.dll",
        "windowsbase.dll",
        "zvec.desktop.deps.json",
        "zvec.desktop.dll",
        "zvec.desktop.runtimeconfig.json",
    }
)
_FORBIDDEN_SOURCE_SUFFIXES = frozenset(
    {".cs", ".csproj", ".ps1", ".psd1", ".psm1", ".sln", ".xaml"}
)


class PreviewPackagingError(RuntimeError):
    """Raised when a preview build or its payload violates the release contract."""


@dataclass(frozen=True, slots=True)
class BuildPlan:
    """Resolved, side-effect-free description of one preview build."""

    repository_root: Path
    version: str
    spec_path: Path
    icon_path: Path
    nsis_path: Path
    output_root: Path
    payload_directory: Path
    work_directory: Path
    installer_output: Path
    portable_output: Path
    pyinstaller_command: tuple[str, ...]
    makensis_command: tuple[str, ...] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "product": "Zvec Desktop Python Preview",
            "version": self.version,
            "target_runtime": TARGET_RUNTIME,
            "target_python": TARGET_PYTHON,
            "repository_root": str(self.repository_root),
            "spec_path": str(self.spec_path),
            "icon_path": str(self.icon_path),
            "nsis_path": str(self.nsis_path),
            "output_root": str(self.output_root),
            "payload_directory": str(self.payload_directory),
            "work_directory": str(self.work_directory),
            "installer_output": str(self.installer_output),
            "portable_output": str(self.portable_output),
            "entry_points": list(ENTRY_POINTS),
            "excluded_packages": sorted(EXCLUDED_PACKAGES),
            "pyinstaller_available": importlib.util.find_spec("PyInstaller")
            is not None,
            "host": {
                "implementation": platform.python_implementation(),
                "python": platform.python_version(),
                "machine": platform.machine(),
                "platform": sys.platform,
            },
            "commands": {
                "pyinstaller": list(self.pyinstaller_command),
                "makensis": (
                    list(self.makensis_command)
                    if self.makensis_command is not None
                    else None
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class PayloadInspection:
    """Deterministic result of checking a built onedir payload."""

    files: tuple[Path, ...]
    errors: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def require_valid(self) -> None:
        if self.errors:
            raise PreviewPackagingError("\n".join(self.errors))


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_project_version(root: Path) -> str:
    project_path = root / "pyproject.toml"
    try:
        content = project_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreviewPackagingError(f"Unable to read {project_path}: {exc}") from exc
    match = _PROJECT_VERSION.search(content)
    if match is None:
        raise PreviewPackagingError("pyproject.toml does not define project.version")
    version = match.group("version").strip()
    if not version or any(character in version for character in "\r\n\0"):
        raise PreviewPackagingError("pyproject.toml contains an unsafe version")
    return version


def windows_file_version(version: str) -> str:
    numbers = [int(value) for value in _WINDOWS_FILE_VERSION_PART.findall(version)]
    if len(numbers) < 3:
        raise PreviewPackagingError(
            f"Version must contain at least three numeric parts: {version}"
        )
    selected = (numbers + [0, 0, 0, 0])[:4]
    if any(value > 65535 for value in selected):
        raise PreviewPackagingError("Windows file-version parts must not exceed 65535")
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
        else resolved_root / "dist" / "python-preview" / version / TARGET_RUNTIME
    )
    work_root = (
        work_directory.resolve()
        if work_directory is not None
        else resolved_root / "build" / "python-preview"
    )
    resolved_work = work_root / TARGET_RUNTIME
    spec_path = (
        resolved_root / "release" / "python_preview" / ("zvec_python_preview.spec")
    )
    payload_directory = resolved_output / PRODUCT_DIRECTORY
    installer_output = resolved_output / (
        f"Zvec-Desktop-Python-Preview-{version}-{TARGET_RUNTIME}-unsigned-setup.exe"
    )
    portable_output = resolved_output / (
        f"Zvec-Desktop-Python-Preview-{version}-{TARGET_RUNTIME}-portable.zip"
    )
    pyinstaller_command = (
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
    makensis_command: tuple[str, ...] | None = None
    if makensis is not None:
        resolved_makensis = makensis.resolve()
        makensis_command = (
            str(resolved_makensis),
            f"/DVERSION={version}",
            f"/DFILE_VERSION={windows_file_version(version)}",
            f"/DSOURCE_DIR={payload_directory}",
            f"/DOUTPUT_FILE={installer_output}",
            str(resolved_root / "installer" / "Zvec.PythonPreview.nsi"),
        )
    return BuildPlan(
        repository_root=resolved_root,
        version=version,
        spec_path=spec_path,
        icon_path=resolved_root
        / "desktop"
        / "Zvec.Desktop"
        / "Assets"
        / "Zvec.AppIcon.ico",
        nsis_path=resolved_root / "installer" / "Zvec.PythonPreview.nsi",
        output_root=resolved_output,
        payload_directory=payload_directory,
        work_directory=resolved_work,
        installer_output=installer_output,
        portable_output=portable_output,
        pyinstaller_command=pyinstaller_command,
        makensis_command=makensis_command,
    )


def validate_static_inputs(plan: BuildPlan) -> None:
    required_files = (
        plan.repository_root / "pyproject.toml",
        plan.repository_root / "model-catalog.default.json",
        plan.repository_root / "release" / "python_preview" / "frozen_entry.py",
        plan.repository_root / "release" / "python_preview" / "runtime_hook.py",
        plan.repository_root / "release" / "python_preview" / "hooks" / "hook-zvec.py",
        plan.spec_path,
        plan.icon_path,
        plan.nsis_path,
    )
    missing = [str(path) for path in required_files if not path.is_file()]
    if missing:
        raise PreviewPackagingError(
            "Python preview build inputs are missing:\n" + "\n".join(missing)
        )
    if plan.makensis_command is not None:
        executable = Path(plan.makensis_command[0])
        if not executable.is_file():
            raise PreviewPackagingError(f"makensis.exe was not found: {executable}")


def validate_build_runtime() -> None:
    errors: list[str] = []
    if os.name != "nt" or sys.platform != "win32":
        errors.append("The preview payload must be built on Windows.")
    if platform.python_implementation() != "CPython":
        errors.append("The preview payload must be built with CPython.")
    if sys.version_info[:2] != (3, 12):
        errors.append(
            "The preview payload is pinned to CPython 3.12; "
            f"the active interpreter is {platform.python_version()}."
        )
    if struct.calcsize("P") != 8 or platform.machine().casefold() not in {
        "amd64",
        "x86_64",
    }:
        errors.append("The preview payload requires a native Windows x64 process.")
    if importlib.util.find_spec("PyInstaller") is None:
        errors.append(
            "PyInstaller is not installed. Prepare the offline build environment "
            "from requirements-packaging.txt; this script will not install it."
        )
    else:
        try:
            installed_pyinstaller = importlib.metadata.version("PyInstaller")
        except importlib.metadata.PackageNotFoundError:
            errors.append("PyInstaller package metadata is unavailable.")
        else:
            if installed_pyinstaller != PYINSTALLER_VERSION:
                errors.append(
                    f"PyInstaller {PYINSTALLER_VERSION} is required; "
                    f"found {installed_pyinstaller}."
                )
    if errors:
        raise PreviewPackagingError("\n".join(errors))


def inspect_payload(payload_directory: Path) -> PayloadInspection:
    supplied_root = payload_directory.expanduser().absolute()
    root = supplied_root.resolve()
    errors: list[str] = []
    if _is_link_or_reparse_point(supplied_root):
        errors.append(
            f"Preview payload must not be a link or reparse point: {supplied_root}"
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
    relative = {path.relative_to(root).as_posix(): path for path in files}
    folded_names = {path.name.casefold() for path in files}

    for entry_point in ENTRY_POINTS:
        path = root / entry_point
        if not path.is_file() or path.is_symlink():
            errors.append(f"Required executable is missing: {entry_point}")
        elif path.stat().st_size <= 0:
            errors.append(f"Required executable is empty: {entry_point}")

    for path in files:
        rel = path.relative_to(root)
        folded_parts = tuple(part.casefold() for part in rel.parts)
        folded_name = path.name.casefold()
        if path.suffix.casefold() in _FORBIDDEN_SOURCE_SUFFIXES:
            errors.append(
                f"Forbidden source/runtime file is packaged: {rel.as_posix()}"
            )
        if folded_name in _DOTNET_RUNTIME_FILES:
            errors.append(f".NET/WPF runtime file is packaged: {rel.as_posix()}")
        if folded_name.endswith((".deps.json", ".runtimeconfig.json")):
            errors.append(f".NET application metadata is packaged: {rel.as_posix()}")
        if path.suffix.casefold() == ".dll" and folded_name.startswith(
            ("microsoft.", "system.")
        ):
            errors.append(f".NET framework assembly is packaged: {rel.as_posix()}")
        for package in EXCLUDED_PACKAGES:
            if any(
                part == package
                or part.startswith(f"{package}-")
                and part.endswith((".dist-info", ".data"))
                for part in folded_parts
            ):
                errors.append(
                    f"Excluded package {package!r} is packaged: {rel.as_posix()}"
                )

    required_named_files = (
        "python312.dll",
        "_tkinter.pyd",
        "tcl86t.dll",
        "tk86t.dll",
        "jieba.dict.utf8",
        "hmm_model.utf8",
        "init.tcl",
        "tk.tcl",
        "model-catalog.default.json",
    )
    for required_name in required_named_files:
        if required_name.casefold() not in folded_names:
            errors.append(f"Required runtime file is missing: {required_name}")

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
    if not relative:
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


def write_payload_manifest(
    payload_directory: Path,
    *,
    version: str,
    pyinstaller_version: str,
) -> Path:
    inspection = inspect_payload(payload_directory)
    inspection.require_valid()
    root = payload_directory.resolve()
    entries = []
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
        "product": "Zvec Desktop Python Preview",
        "version": version,
        "target_runtime": TARGET_RUNTIME,
        "python_runtime": TARGET_PYTHON,
        "pyinstaller_version": pyinstaller_version,
        "entry_points": list(ENTRY_POINTS),
        "excluded_packages": sorted(EXCLUDED_PACKAGES),
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
        raise PreviewPackagingError(f"Unable to write payload manifest: {exc}") from exc
    return manifest_path


def verify_payload_manifest(payload_directory: Path) -> dict[str, Any]:
    root = payload_directory.resolve()
    inspection = inspect_payload(root)
    inspection.require_valid()
    manifest_path = root / MANIFEST_FILE
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise PreviewPackagingError(
                "Preview payload manifest is unexpectedly large."
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except PreviewPackagingError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreviewPackagingError(f"Unable to read payload manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PreviewPackagingError("Preview payload manifest must be a JSON object.")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise PreviewPackagingError("Unsupported preview payload manifest schema.")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise PreviewPackagingError("Preview payload manifest files must be a list.")

    expected_paths = {
        path.relative_to(root).as_posix()
        for path in inspection.files
        if path.name != MANIFEST_FILE
    }
    manifest_paths: set[str] = set()
    total_size = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise PreviewPackagingError(
                "Preview payload manifest has an invalid entry."
            )
        relative = entry.get("path")
        size = entry.get("size")
        expected_hash = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
        ):
            raise PreviewPackagingError("Preview payload manifest has an unsafe path.")
        if relative in manifest_paths:
            raise PreviewPackagingError(f"Duplicate manifest path: {relative}")
        manifest_paths.add(relative)
        path = root / Path(relative)
        if not path.is_file() or path.is_symlink():
            raise PreviewPackagingError(f"Manifest file is missing: {relative}")
        actual_size = path.stat().st_size
        if not isinstance(size, int) or isinstance(size, bool) or size != actual_size:
            raise PreviewPackagingError(f"Manifest size mismatch: {relative}")
        if not isinstance(expected_hash, str) or expected_hash != _sha256(path):
            raise PreviewPackagingError(f"Manifest SHA-256 mismatch: {relative}")
        total_size += actual_size

    if manifest_paths != expected_paths:
        missing = sorted(expected_paths - manifest_paths)
        unexpected = sorted(manifest_paths - expected_paths)
        raise PreviewPackagingError(
            "Manifest file set does not match the payload. "
            f"Missing={missing}; unexpected={unexpected}"
        )
    if manifest.get("file_count") != len(entries):
        raise PreviewPackagingError("Manifest file_count does not match files.")
    if manifest.get("total_size") != total_size:
        raise PreviewPackagingError("Manifest total_size does not match files.")
    return manifest


def write_portable_archive(
    payload_directory: Path,
    archive_path: Path,
) -> dict[str, Any]:
    """Create and verify the portable ZIP beside an already verified payload."""

    root = payload_directory.resolve()
    verify_payload_manifest(root)
    inspection = inspect_payload(root)
    inspection.require_valid()
    output = archive_path.expanduser().absolute()
    if output == root or root in output.parents:
        raise PreviewPackagingError("Portable archive must be outside the payload.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    expected_names = tuple(
        path.relative_to(root).as_posix() for path in inspection.files
    )
    try:
        temporary.unlink(missing_ok=True)
        with zipfile.ZipFile(
            temporary,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            allowZip64=True,
        ) as archive:
            for path, relative in zip(inspection.files, expected_names, strict=True):
                archive.write(path, relative)
        with zipfile.ZipFile(temporary, mode="r") as archive:
            actual_names = tuple(info.filename for info in archive.infolist())
            if actual_names != expected_names:
                raise PreviewPackagingError(
                    "Portable archive file set does not match the payload."
                )
            damaged = archive.testzip()
            if damaged is not None:
                raise PreviewPackagingError(
                    f"Portable archive CRC verification failed: {damaged}"
                )
        os.replace(temporary, output)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, PreviewPackagingError):
            raise
        raise PreviewPackagingError(
            f"Unable to create portable archive: {exc}"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": str(output),
        "file_count": len(expected_names),
        "size": output.stat().st_size,
        "sha256": _sha256(output),
    }
