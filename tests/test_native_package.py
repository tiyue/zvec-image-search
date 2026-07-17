from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class NativePackageTest(unittest.TestCase):
    def test_built_wheel_installs_zvec_command_entrypoint(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        root = Path(tempfile.mkdtemp(prefix="zvec_native_wheel_test_"))
        try:
            wheels = root / "wheels"
            environment = root / "venv"
            wheels.mkdir()
            build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--wheel-dir",
                    str(wheels),
                    str(repository),
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

            subprocess.run(
                [sys.executable, "-m", "venv", str(environment)],
                check=True,
                capture_output=True,
                text=True,
            )
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
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
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
