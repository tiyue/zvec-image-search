from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


class NativePackageTest(unittest.TestCase):
    def test_built_wheel_installs_zvec_command_entrypoint(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        root = Path(tempfile.mkdtemp(prefix="zvec_native_wheel_test_"))
        try:
            wheels = root / "wheels"
            environment = root / "venv"
            source = root / "source"
            wheels.mkdir()
            source.mkdir()
            # Build from a clean staging tree so an unrelated local setuptools
            # build directory can never leak stale files into the wheel.
            for name in (
                "pyproject.toml",
                "README.md",
                "image_service.py",
                "zvec_launcher.py",
                "zvec_logging.py",
            ):
                shutil.copy2(repository / name, source / name)
            for name in ("image_vector_service", "zvec_desktop", "zvec_webview"):
                shutil.copytree(
                    repository / name,
                    source / name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )

            # `uv run` intentionally creates a minimal interpreter that may not
            # expose pip in the parent environment.  Build and install through a
            # dedicated venv so this packaging test has the same deterministic
            # bootstrap behavior locally and in CI.
            subprocess.run(
                [sys.executable, "-m", "venv", str(environment)],
                check=True,
                capture_output=True,
                text=True,
            )
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            build = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--wheel-dir",
                    str(wheels),
                    str(source),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(build.returncode, 0, build.stderr or build.stdout)
            wheel_files = list(wheels.glob("*.whl"))
            self.assertEqual(len(wheel_files), 1)
            with zipfile.ZipFile(wheel_files[0]) as wheel:
                names = set(wheel.namelist())
                manifests = [
                    name
                    for name in names
                    if name.endswith("zvec_webview/frontend_dist/.vite/manifest.json")
                ]
                self.assertEqual(len(manifests), 1)
                manifest = json.loads(wheel.read(manifests[0]).decode("utf-8"))
                entry = manifest["index.html"]
                frontend_prefix = manifests[0].removesuffix(".vite/manifest.json")
                self.assertIn(frontend_prefix + "index.html", names)
                self.assertIn(frontend_prefix + entry["file"], names)
                for stylesheet in entry.get("css", []):
                    self.assertIn(frontend_prefix + stylesheet, names)
                self.assertFalse(
                    any("node_modules" in name.casefold() for name in names)
                )
                self.assertFalse(
                    any("zvec_webview/assets/" in name.casefold() for name in names)
                )

            install = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    str(wheel_files[0]),
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(install.returncode, 0, install.stderr or install.stdout)
            command = environment / (
                "Scripts/zvec.exe" if os.name == "nt" else "bin/zvec"
            )
            invocation = subprocess.run(
                [str(command), "help"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(
                invocation.returncode, 0, invocation.stderr or invocation.stdout
            )
            self.assertIn("Zvec native launcher", invocation.stdout)
            self.assertIn("Docker is not required", invocation.stdout)

            desktop_command = environment / (
                "Scripts/zvec-desktop.exe" if os.name == "nt" else "bin/zvec-desktop"
            )
            desktop_help = subprocess.run(
                [str(desktop_command), "--help"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(
                desktop_help.returncode,
                0,
                desktop_help.stderr or desktop_help.stdout,
            )
            self.assertIn("pure-Python image library desktop", desktop_help.stdout)
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
