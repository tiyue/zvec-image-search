"""Per-user Task Scheduler integration for the installed WebView host."""

from __future__ import annotations

import configparser
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

PRODUCT_ID: Final = "{7A99D348-73DF-49EA-AF2B-8D86FC6DA2A8}"
PRODUCT_MARKER: Final = ".zvec-webview-preview-install.ini"
PRODUCT_DIRECTORY: Final = "YaoLens"
MAIN_EXECUTABLE: Final = "YaoLens.exe"
TASK_NAME: Final = r"\YaoLens\Resident"
LEGACY_TASK_NAME: Final = r"\Zvec\WebviewPreviewResident"
START_HIDDEN_ARGUMENT: Final = "--start-hidden"
RESTART_INTERVAL: Final = "PT1M"
RESTART_COUNT: Final = 3

_TASK_NAMESPACE: Final = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_SID_PATTERN: Final = re.compile(r"\bS-\d+(?:-\d+){2,}\b", re.IGNORECASE)
_CREATE_NO_WINDOW: Final = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_COMMAND_TIMEOUT_SECONDS: Final = 30
_TASK_NOT_FOUND_EXIT_CODE: Final = 1
_TASK_NOT_FOUND_MARKERS: Final = (
    "the system cannot find the file specified",
    "系统找不到指定的文件",
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class ResidentTaskError(RuntimeError):
    """The installed per-user resident task could not be managed safely."""


def default_install_directory(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Return the fixed current-user installation directory."""

    values = os.environ if environment is None else environment
    local_app_data = values.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise ResidentTaskError("LOCALAPPDATA is required to locate the installation.")
    return Path(local_app_data).expanduser().absolute() / "Programs" / PRODUCT_DIRECTORY


def resolve_current_user_sid(
    *,
    runner: CommandRunner = subprocess.run,
    system_root: str | Path | None = None,
) -> str:
    """Resolve the current interactive user's SID with the Windows system tool."""

    whoami = _system_tool("whoami.exe", system_root=system_root)
    completed = _run(
        runner,
        (str(whoami), "/user", "/fo", "csv", "/nh"),
        operation="resolve the current user SID",
    )
    _require_success(completed, operation="resolve the current user SID")
    match = _SID_PATTERN.search(completed.stdout or "")
    if match is None:
        raise ResidentTaskError("whoami.exe did not return a valid current-user SID.")
    return _validated_sid(match.group(0))


def build_task_xml(
    executable: str | Path,
    install_directory: str | Path,
    user_sid: str,
) -> bytes:
    """Build the Task Scheduler XML for one current-user resident host."""

    executable_path = Path(executable).expanduser().absolute()
    working_directory = Path(install_directory).expanduser().absolute()
    sid = _validated_sid(user_sid)

    ET.register_namespace("", _TASK_NAMESPACE)
    task = ET.Element(_tag("Task"), {"version": "1.4"})
    registration = ET.SubElement(task, _tag("RegistrationInfo"))
    ET.SubElement(
        registration, _tag("Description")
    ).text = "Starts YaoLens after the current user logs on."

    triggers = ET.SubElement(task, _tag("Triggers"))
    logon = ET.SubElement(triggers, _tag("LogonTrigger"))
    ET.SubElement(logon, _tag("Enabled")).text = "true"
    ET.SubElement(logon, _tag("UserId")).text = sid

    principals = ET.SubElement(task, _tag("Principals"))
    principal = ET.SubElement(principals, _tag("Principal"), {"id": "CurrentUser"})
    ET.SubElement(principal, _tag("UserId")).text = sid
    ET.SubElement(principal, _tag("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, _tag("RunLevel")).text = "LeastPrivilege"

    settings = ET.SubElement(task, _tag("Settings"))
    ET.SubElement(settings, _tag("MultipleInstancesPolicy")).text = "IgnoreNew"
    ET.SubElement(settings, _tag("DisallowStartIfOnBatteries")).text = "false"
    ET.SubElement(settings, _tag("StopIfGoingOnBatteries")).text = "false"
    ET.SubElement(settings, _tag("StartWhenAvailable")).text = "true"
    ET.SubElement(settings, _tag("AllowStartOnDemand")).text = "true"
    ET.SubElement(settings, _tag("Enabled")).text = "true"
    ET.SubElement(settings, _tag("ExecutionTimeLimit")).text = "PT0S"
    restart = ET.SubElement(settings, _tag("RestartOnFailure"))
    ET.SubElement(restart, _tag("Interval")).text = RESTART_INTERVAL
    ET.SubElement(restart, _tag("Count")).text = str(RESTART_COUNT)

    actions = ET.SubElement(task, _tag("Actions"), {"Context": "CurrentUser"})
    execute = ET.SubElement(actions, _tag("Exec"))
    ET.SubElement(execute, _tag("Command")).text = str(executable_path)
    ET.SubElement(execute, _tag("Arguments")).text = START_HIDDEN_ARGUMENT
    ET.SubElement(execute, _tag("WorkingDirectory")).text = str(working_directory)
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def install_resident_task(
    install_directory: str | Path,
    *,
    user_sid: str | None = None,
    runner: CommandRunner = subprocess.run,
    system_root: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    temp_directory: str | Path | None = None,
) -> None:
    """Create or refresh the installed current-user logon task."""

    expected = default_install_directory(environment)
    installed, executable = _validated_installation(install_directory, expected)
    sid = user_sid or resolve_current_user_sid(
        runner=runner,
        system_root=system_root,
    )
    task_xml = build_task_xml(executable, installed, sid)
    schtasks = _system_tool("schtasks.exe", system_root=system_root)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="yaolens-resident-task-",
            suffix=".xml",
            dir=temp_directory,
            delete=False,
        ) as stream:
            stream.write(task_xml)
            temporary_path = Path(stream.name)
        completed = _run(
            runner,
            (
                str(schtasks),
                "/Create",
                "/TN",
                TASK_NAME,
                "/XML",
                str(temporary_path),
                "/F",
            ),
            operation="install the YaoLens resident task",
        )
        _require_success(completed, operation="install the YaoLens resident task")
        try:
            _remove_named_task(
                schtasks,
                LEGACY_TASK_NAME,
                runner=runner,
                operation="remove the legacy YaoLens resident task",
            )
        except ResidentTaskError as legacy_error:
            try:
                _remove_named_task(
                    schtasks,
                    TASK_NAME,
                    runner=runner,
                    operation="roll back the newly-created YaoLens resident task",
                )
            except ResidentTaskError as rollback_error:
                raise legacy_error from rollback_error
            raise
    except OSError as exc:
        raise ResidentTaskError(
            "Could not create the temporary resident-task definition."
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                raise ResidentTaskError(
                    "Could not remove the temporary resident-task definition."
                ) from exc


def remove_resident_task(
    *,
    runner: CommandRunner = subprocess.run,
    system_root: str | Path | None = None,
) -> bool:
    """Remove current and legacy tasks, returning whether either existed."""

    schtasks = _system_tool("schtasks.exe", system_root=system_root)
    removed = False
    for task_name, operation in (
        (TASK_NAME, "remove the YaoLens resident task"),
        (LEGACY_TASK_NAME, "remove the legacy YaoLens resident task"),
    ):
        removed = (
            _remove_named_task(
                schtasks,
                task_name,
                runner=runner,
                operation=operation,
            )
            or removed
        )
    return removed


def _remove_named_task(
    schtasks: Path,
    task_name: str,
    *,
    runner: CommandRunner,
    operation: str,
) -> bool:
    query = _run(
        runner,
        (str(schtasks), "/Query", "/TN", task_name),
        operation=f"query before {operation}",
    )
    if query.returncode != 0:
        if _task_query_reports_missing(query):
            return False
        _require_success(query, operation=f"query before {operation}")
    deleted = _run(
        runner,
        (str(schtasks), "/Delete", "/TN", task_name, "/F"),
        operation=operation,
    )
    _require_success(deleted, operation=operation)
    return True


def _validated_installation(
    install_directory: str | Path,
    expected_install_directory: Path,
) -> tuple[Path, Path]:
    supplied = Path(install_directory).expanduser().absolute()
    expected = expected_install_directory.expanduser().absolute()
    if os.path.normcase(str(supplied)) != os.path.normcase(str(expected)):
        raise ResidentTaskError(
            f"Resident task registration is restricted to {expected}."
        )
    if not supplied.is_dir() or supplied.is_symlink():
        raise ResidentTaskError(f"Installed YaoLens directory is missing: {supplied}")

    marker = supplied / PRODUCT_MARKER
    if not marker.is_file() or marker.is_symlink():
        raise ResidentTaskError(
            f"Installed YaoLens ownership marker is missing: {marker}"
        )
    product_id = _read_marker_product_id(marker)
    if product_id.casefold() != PRODUCT_ID.casefold():
        raise ResidentTaskError(
            "Installed YaoLens ownership marker has the wrong ProductId."
        )

    executable = supplied / MAIN_EXECUTABLE
    if (
        not executable.is_file()
        or executable.is_symlink()
        or executable.stat().st_size <= 0
    ):
        raise ResidentTaskError(
            f"Installed YaoLens executable is missing: {executable}"
        )
    return supplied, executable


def _read_marker_product_id(marker: Path) -> str:
    try:
        encoded = marker.read_bytes()
        if encoded.startswith((b"\xff\xfe", b"\xfe\xff")):
            content = encoded.decode("utf-16")
        else:
            content = encoded.decode("utf-8-sig")
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(content)
        return parser.get("ZvecWebviewPreview", "ProductId").strip()
    except (OSError, UnicodeError, configparser.Error, KeyError) as exc:
        raise ResidentTaskError(
            f"Installed YaoLens ownership marker is invalid: {marker}"
        ) from exc


def _validated_sid(value: str) -> str:
    sid = value.strip()
    if _SID_PATTERN.fullmatch(sid) is None:
        raise ResidentTaskError("A valid current-user SID is required.")
    return sid


def _system_tool(
    name: str,
    *,
    system_root: str | Path | None,
) -> Path:
    root_value = system_root
    if root_value is None:
        root_value = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    if root_value is None or not str(root_value).strip():
        raise ResidentTaskError("The Windows system directory could not be resolved.")
    tool = Path(root_value).expanduser().absolute() / "System32" / name
    if not tool.is_file() or tool.is_symlink():
        raise ResidentTaskError(f"The Windows system tool is missing: {tool}")
    return tool


def _run(
    runner: CommandRunner,
    command: Sequence[str],
    *,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_COMMAND_TIMEOUT_SECONDS,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ResidentTaskError(f"Could not {operation}.") from exc


def _require_success(
    completed: subprocess.CompletedProcess[str],
    *,
    operation: str,
) -> None:
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "").strip()[-1000:]
    suffix = f": {detail}" if detail else ""
    raise ResidentTaskError(
        f"Could not {operation} (exit code {completed.returncode}){suffix}"
    )


def _task_query_reports_missing(
    completed: subprocess.CompletedProcess[str],
) -> bool:
    if completed.returncode != _TASK_NOT_FOUND_EXIT_CODE:
        return False
    detail = "\n".join((completed.stderr or "", completed.stdout or "")).casefold()
    return any(marker in detail for marker in _TASK_NOT_FOUND_MARKERS)


def _tag(name: str) -> str:
    return f"{{{_TASK_NAMESPACE}}}{name}"


__all__ = [
    "LEGACY_TASK_NAME",
    "MAIN_EXECUTABLE",
    "PRODUCT_ID",
    "PRODUCT_MARKER",
    "RESTART_COUNT",
    "RESTART_INTERVAL",
    "ResidentTaskError",
    "START_HIDDEN_ARGUMENT",
    "TASK_NAME",
    "build_task_xml",
    "default_install_directory",
    "install_resident_task",
    "remove_resident_task",
    "resolve_current_user_sid",
]
