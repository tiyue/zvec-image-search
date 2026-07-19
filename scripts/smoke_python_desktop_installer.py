"""Destructively smoke-test the Python desktop installer on a clean CI account."""

from __future__ import annotations

import argparse
import ctypes
import http.client
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast


class InstallerSmokeError(RuntimeError):
    """The clean-account install, CLI smoke, or uninstall contract failed."""


def _run_cli_help(executable: Path) -> None:
    result = subprocess.run(
        [str(executable), "help"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
    )
    if result.returncode != 0 or "Docker is not required" not in (
        result.stdout + result.stderr
    ):
        raise InstallerSmokeError(f"{executable.name} failed its CLI smoke test")


def _smoke_portable(archive: Path) -> None:
    """Extract and execute the portable CLI without trusting archive paths."""

    resolved = archive.resolve(strict=True)
    with TemporaryDirectory(prefix="zvec-portable-smoke-") as temporary:
        extraction_root = Path(temporary).resolve()
        try:
            with zipfile.ZipFile(resolved, "r") as package:
                for member in package.infolist():
                    target = (extraction_root / member.filename).resolve()
                    try:
                        target.relative_to(extraction_root)
                    except ValueError as exc:
                        raise InstallerSmokeError(
                            "Portable archive contains an unsafe path: "
                            f"{member.filename}"
                        ) from exc
                damaged = package.testzip()
                if damaged is not None:
                    raise InstallerSmokeError(
                        f"Portable archive CRC verification failed: {damaged}"
                    )
                package.extractall(extraction_root)
        except (OSError, zipfile.BadZipFile) as exc:
            raise InstallerSmokeError(f"Portable archive is invalid: {exc}") from exc
        # The current portable archive deliberately opens directly onto the
        # executable payload.  Accept the former one-directory wrapper as a
        # transition, but never guess past these two documented layouts.
        payload = extraction_root
        if not (payload / "zvec.exe").is_file():
            nested_payload = extraction_root / "Zvec-Desktop"
            if not (nested_payload / "zvec.exe").is_file():
                raise InstallerSmokeError(
                    "Portable archive is missing the zvec.exe entry point"
                )
            payload = nested_payload
        _run_cli_help(payload / "zvec.exe")


def _request_json(
    *,
    port: int,
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read()
    finally:
        connection.close()
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallerSmokeError(
            f"Backend {method} {path} returned invalid JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise InstallerSmokeError(f"Backend {method} {path} returned non-object JSON")
    return response.status, decoded


def _smoke_backend(executable: Path, *, config_home: Path) -> None:
    """Start the frozen backend, verify health, then stop it gracefully."""

    image_root = config_home / "smoke-images"
    workspace = config_home / "smoke-workspace"
    results = config_home / "smoke-results"
    query_root = config_home / "query-staging"
    for directory in (image_root, workspace, results, query_root):
        directory.mkdir(parents=True, exist_ok=True)
    libraries_config = config_home / "smoke-libraries.json"
    libraries_config.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "default_library_id": "smoke-library",
                "results_directory": str(results),
                "libraries": [
                    {
                        "id": "smoke-library",
                        "name": "Installer smoke",
                        "image_root": str(image_root),
                        "workspace_directory": str(workspace),
                        "enabled": True,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    token = secrets.token_urlsafe(32)
    environment = os.environ.copy()
    environment["ZVEC_CONFIG_HOME"] = str(config_home)
    command = [
        str(executable),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--token",
        token,
        "--query-root",
        str(query_root),
        "--libraries-config",
        str(libraries_config),
        "--instance-id",
        "installer-smoke",
        "--config-fingerprint",
        "installer-smoke",
        "--instance-lock-path",
        str(config_home / "smoke-backend.lock"),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        creationflags=creationflags,
    )
    output: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        stream = process.stdout
        if stream is None:
            return
        for line in stream:
            output.put(line.rstrip())

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    port: int | None = None
    captured: list[str] = []
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and port is None:
            if process.poll() is not None and output.empty():
                break
            try:
                line = output.get(timeout=0.2)
            except queue.Empty:
                continue
            captured.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(event, dict)
                and event.get("event") == "backend_listening"
                and isinstance(event.get("port"), int)
            ):
                port = int(event["port"])
        if port is None:
            detail = " | ".join(captured[-8:]) or "no backend output"
            raise InstallerSmokeError(f"Frozen backend did not listen: {detail}")

        health_deadline = time.monotonic() + 45
        last_health: dict[str, Any] = {}
        while time.monotonic() < health_deadline:
            try:
                status, last_health = _request_json(
                    port=port,
                    method="GET",
                    path="/health",
                    token=token,
                )
            except (OSError, InstallerSmokeError):
                time.sleep(0.2)
                continue
            if (
                status == 200
                and last_health.get("service_ready") is True
                and last_health.get("worker_alive") is True
            ):
                break
            time.sleep(0.2)
        else:
            raise InstallerSmokeError(
                f"Frozen backend never became healthy: {last_health}"
            )

        status, response = _request_json(
            port=port,
            method="POST",
            path="/v1/control/shutdown",
            token=token,
            payload={"if_idle": True},
        )
        if status != 202 or response.get("accepted") is not True:
            raise InstallerSmokeError(
                f"Frozen backend rejected graceful shutdown: {status} {response}"
            )
        process.wait(timeout=20)
        if process.returncode != 0:
            raise InstallerSmokeError(
                f"Frozen backend exited with code {process.returncode}"
            )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        reader.join(timeout=2)


def _wait_for_absence(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not path.exists():
            return
        time.sleep(0.1)
    raise InstallerSmokeError(f"Uninstall did not remove {path}")


def _run_installer(
    installer: Path,
    *,
    environment: dict[str, str],
    expected_exit_code: int,
) -> None:
    completed = subprocess.run(
        [str(installer), "/S"],
        check=False,
        timeout=120,
        env=environment,
    )
    # NSIS may briefly retain the executable image after the parent process exits.
    time.sleep(0.15)
    if completed.returncode != expected_exit_code:
        raise InstallerSmokeError(
            f"Installer returned {completed.returncode}; expected {expected_exit_code}"
        )


@contextmanager
def _exclusive_launch_lock(path: Path) -> Iterator[None]:
    # Linux type stubs intentionally omit Windows-only ctypes members even
    # though this smoke helper is guarded by ``os.name == "nt"`` at runtime.
    windows_ctypes = cast(Any, ctypes)
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = create_file(
        str(path),
        0xC0000000,
        0,
        None,
        2,
        0x80,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in {None, invalid_handle}:
        raise InstallerSmokeError(
            f"Unable to create exclusive launch lock: {windows_ctypes.get_last_error()}"
        )
    try:
        yield
    finally:
        close_handle(handle)


@contextmanager
def _backend_byte_lock(path: Path) -> Iterator[None]:
    import msvcrt

    # See ``_exclusive_launch_lock``: the Linux Mypy pass cannot expose the
    # Windows locking API, while the production execution path is Windows-only.
    windows_msvcrt = cast(Any, msvcrt)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        windows_msvcrt.locking(stream.fileno(), windows_msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            stream.seek(0)
            windows_msvcrt.locking(stream.fileno(), windows_msvcrt.LK_UNLCK, 1)


def smoke_installer(
    installer: Path,
    *,
    portable_archive: Path | None = None,
    require_clean_ci: bool = True,
) -> None:
    if os.name != "nt":
        raise InstallerSmokeError("Installer smoke is supported only on Windows")
    if require_clean_ci and os.getenv("CI", "").casefold() != "true":
        raise InstallerSmokeError(
            "Refusing destructive installer smoke outside an explicit CI account"
        )
    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        raise InstallerSmokeError("LOCALAPPDATA is unavailable")
    install_dir = Path(local_app_data) / "Programs" / "Zvec Desktop"
    if install_dir.exists():
        raise InstallerSmokeError(
            "Refusing destructive smoke because Zvec Desktop is already installed"
        )
    resolved_installer = installer.resolve(strict=True)
    if portable_archive is not None:
        _smoke_portable(portable_archive)
    with TemporaryDirectory(prefix="zvec-installer-smoke-") as temporary:
        config_home = Path(temporary) / "config"
        backend_home = config_home / "backend"
        backend_home.mkdir(parents=True)
        sentinel = config_home / "user-data-must-survive.txt"
        sentinel.write_text("preserve", encoding="utf-8")
        environment = os.environ.copy()
        environment["ZVEC_CONFIG_HOME"] = str(config_home)

        with _exclusive_launch_lock(backend_home / "launch.lock"):
            _run_installer(
                resolved_installer,
                environment=environment,
                expected_exit_code=35,
            )
        if install_dir.exists():
            raise InstallerSmokeError("Launch-lock guard changed the install directory")

        with _backend_byte_lock(backend_home / "backend.lock"):
            _run_installer(
                resolved_installer,
                environment=environment,
                expected_exit_code=35,
            )
        if install_dir.exists():
            raise InstallerSmokeError(
                "Backend-lock guard changed the install directory"
            )

        _run_installer(
            resolved_installer,
            environment=environment,
            expected_exit_code=0,
        )
        required = (
            install_dir / "Zvec.Desktop.exe",
            install_dir / "zvec-backend.exe",
            install_dir / "zvec.exe",
            install_dir / "desktop-payload-manifest.json",
            install_dir / "Uninstall.exe",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise InstallerSmokeError(
                "Installed files are missing: " + ", ".join(missing)
            )
        forbidden = [
            path
            for path in install_dir.rglob("*")
            if path.is_file()
            and (
                path.suffix.casefold() in {".ps1", ".cs", ".csproj", ".xaml"}
                or path.name.casefold()
                in {"zvec.desktop.dll", "zvec.desktop.runtimeconfig.json"}
            )
        ]
        if forbidden:
            raise InstallerSmokeError(
                "Forbidden legacy files were installed: "
                + ", ".join(str(path) for path in forbidden)
            )
        _run_cli_help(install_dir / "zvec.exe")
        _smoke_backend(install_dir / "zvec-backend.exe", config_home=config_home)

        uninstaller = install_dir / "Uninstall.exe"
        uninstall = subprocess.run(
            [str(uninstaller), "/S"],
            check=False,
            timeout=120,
            env=environment,
        )
        if uninstall.returncode != 0:
            raise InstallerSmokeError(
                f"Silent uninstaller returned exit code {uninstall.returncode}"
            )
        _wait_for_absence(install_dir)
        if sentinel.read_text(encoding="utf-8") != "preserve":
            raise InstallerSmokeError("Uninstall modified the configured user data")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--installer", type=Path)
    source.add_argument(
        "--output-root",
        type=Path,
        help="Directory containing exactly one unsigned Zvec Desktop installer.",
    )
    return parser


def _resolve_installer(installer: Path | None, output_root: Path | None) -> Path:
    if installer is not None:
        return installer
    assert output_root is not None
    matches = sorted(
        {
            *output_root.glob("Zvec-Desktop-*-win-x64-unsigned-setup.exe"),
            *output_root.glob("Zvec-Desktop-*-win-x64-setup.exe"),
        }
    )
    if len(matches) != 1:
        raise InstallerSmokeError(
            "Expected exactly one desktop installer in "
            f"{output_root}; found {len(matches)}"
        )
    return matches[0]


def _resolve_portable(output_root: Path | None) -> Path | None:
    if output_root is None:
        return None
    matches = sorted(output_root.glob("Zvec-Desktop-*-win-x64-portable.zip"))
    if len(matches) != 1:
        raise InstallerSmokeError(
            "Expected exactly one portable desktop archive in "
            f"{output_root}; found {len(matches)}"
        )
    return matches[0]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        smoke_installer(
            _resolve_installer(args.installer, args.output_root),
            portable_archive=_resolve_portable(args.output_root),
        )
    except (OSError, InstallerSmokeError, subprocess.SubprocessError) as exc:
        print(f"Installer smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
