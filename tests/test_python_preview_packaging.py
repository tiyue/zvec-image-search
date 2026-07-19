from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from release.python_preview import frozen_entry
from scripts import build_python_desktop, build_python_preview
from scripts.python_preview_packaging import (
    ENTRY_POINTS,
    PreviewPackagingError,
    create_build_plan,
    inspect_payload,
    repository_root,
    validate_static_inputs,
    verify_payload_manifest,
    windows_file_version,
    write_payload_manifest,
    write_portable_archive,
)
from scripts.smoke_python_desktop_installer import (
    _backend_byte_lock,
    _exclusive_launch_lock,
    _run_installer,
    _smoke_portable,
)


def _write(root: Path, relative: str, content: bytes = b"desktop") -> None:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _valid_payload(root: Path) -> Path:
    payload = root / "Zvec-Desktop"
    for entry_point in ENTRY_POINTS:
        _write(payload, entry_point)
    for relative in (
        "model-catalog.default.json",
        "assets/Zvec.AppIcon.ico",
        "_internal/python312.dll",
        "_internal/_tkinter.pyd",
        "_internal/tcl86t.dll",
        "_internal/tk86t.dll",
        "_internal/_tcl_data/init.tcl",
        "_internal/_tk_data/tk.tcl",
        "_internal/PIL/_imaging.cp312-win_amd64.pyd",
        "_internal/zvec/_zvec.cp312-win_amd64.pyd",
        "_internal/zvec/data/jieba_dict/jieba.dict.utf8",
        "_internal/zvec/data/jieba_dict/hmm_model.utf8",
    ):
        _write(payload, relative)
    return payload


class PythonDesktopPayloadTest(unittest.TestCase):
    def test_valid_payload_and_manifest_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = _valid_payload(Path(temporary))
            inspection = inspect_payload(payload)
            self.assertTrue(inspection.is_valid, inspection.errors)

            manifest_path = write_payload_manifest(
                payload,
                version="0.4.0",
                pyinstaller_version="6.16.0",
            )
            self.assertEqual(manifest_path.name, "desktop-payload-manifest.json")
            manifest = verify_payload_manifest(payload)
            self.assertEqual(manifest["file_count"], len(inspection.files))
            self.assertEqual(manifest["entry_points"], list(ENTRY_POINTS))
            self.assertEqual(
                manifest["excluded_packages"], ["openai", "torch", "transformers"]
            )

    def test_manifest_detects_tampered_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = _valid_payload(Path(temporary))
            write_payload_manifest(
                payload,
                version="0.4.0",
                pyinstaller_version="6.16.0",
            )
            (payload / "zvec.exe").write_bytes(b"changed")
            with self.assertRaisesRegex(PreviewPackagingError, "SHA-256 mismatch"):
                verify_payload_manifest(payload)

    def test_portable_archive_contains_the_verified_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _valid_payload(root)
            write_payload_manifest(
                payload,
                version="0.4.0",
                pyinstaller_version="6.16.0",
            )
            archive_path = root / "desktop-portable.zip"

            result = write_portable_archive(payload, archive_path)

            self.assertEqual(result["path"], str(archive_path.absolute()))
            self.assertGreater(result["size"], 0)
            self.assertEqual(len(result["sha256"]), 64)
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                self.assertIn("Zvec.Desktop.exe", names)
                self.assertIn("desktop-payload-manifest.json", names)
                self.assertIsNone(archive.testzip())

    def test_portable_smoke_accepts_the_builders_flat_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _valid_payload(root)
            write_payload_manifest(
                payload,
                version="0.4.0",
                pyinstaller_version="6.16.0",
            )
            archive_path = root / "desktop-portable.zip"
            write_portable_archive(payload, archive_path)

            with patch(
                "scripts.smoke_python_desktop_installer._run_cli_help"
            ) as run_cli:
                _smoke_portable(archive_path)

            run_cli.assert_called_once()
            self.assertEqual(run_cli.call_args.args[0].name, "zvec.exe")
            self.assertNotEqual(run_cli.call_args.args[0].parent.name, "Zvec-Desktop")

    def test_payload_rejects_legacy_and_external_runtime_files(self) -> None:
        cases = (
            ("scripts/zvec.ps1", "Forbidden"),
            ("Dockerfile", "Forbidden external runtime"),
            ("deploy/Dockerfile.windows", "Forbidden external runtime"),
            ("deploy/compose.yml", "Forbidden external runtime"),
            ("deploy/compose.yaml", "Forbidden external runtime"),
            ("deploy/compose.production.yaml", "Forbidden external runtime"),
            ("deploy/docker-compose.yml", "Forbidden external runtime"),
            ("_internal/docker.exe", "Forbidden external runtime"),
            ("_internal/PowerShell.exe", "Forbidden external runtime"),
            ("_internal/pwsh.exe", "Forbidden external runtime"),
            ("_internal/dotnet.exe", "Forbidden external runtime"),
            ("_internal/Zvec.Desktop.dll", ".NET/WPF"),
            ("_internal/coreclr.dll", ".NET/WPF"),
            ("_internal/hostfxr.dll", ".NET/WPF"),
            ("_internal/hostpolicy.dll", ".NET/WPF"),
            ("_internal/PresentationFramework.dll", ".NET/WPF"),
            ("_internal/WindowsBase.dll", ".NET/WPF"),
            ("_internal/System.Text.Json.dll", ".NET framework"),
            ("_internal/other.runtimeconfig.json", ".NET application metadata"),
        )
        for relative, expected in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                payload = _valid_payload(Path(temp))
                _write(payload, relative)
                inspection = inspect_payload(payload)
                self.assertFalse(inspection.is_valid)
                self.assertIn(expected, "\n".join(inspection.errors))

    def test_payload_rejects_large_optional_packages(self) -> None:
        cases = (
            ("_internal/torch/__init__.pyc", "Excluded package"),
            ("_internal/transformers/data.bin", "Excluded package"),
            ("_internal/openai-1.0.dist-info/METADATA", "Excluded package"),
        )
        for relative, expected in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                payload = _valid_payload(Path(temp))
                _write(payload, relative)
                inspection = inspect_payload(payload)
                self.assertFalse(inspection.is_valid)
                self.assertIn(expected, "\n".join(inspection.errors))

    def test_payload_requires_tk_pillow_zvec_jieba_and_runtime_icon(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = _valid_payload(Path(temporary))
            (payload / "_internal/_tkinter.pyd").unlink()
            (payload / "_internal/PIL/_imaging.cp312-win_amd64.pyd").unlink()
            (payload / "_internal/zvec/data/jieba_dict/jieba.dict.utf8").unlink()
            (payload / "assets/Zvec.AppIcon.ico").unlink()
            errors = "\n".join(inspect_payload(payload).errors)
            self.assertIn("_tkinter.pyd", errors)
            self.assertIn("_imaging", errors)
            self.assertIn("jieba.dict.utf8", errors)
            self.assertIn("Zvec.AppIcon.ico", errors)


class PythonDesktopBuildContractTest(unittest.TestCase):
    def test_build_plan_uses_primary_product_identity_and_independent_icon(
        self,
    ) -> None:
        plan = create_build_plan()
        validate_static_inputs(plan)
        self.assertEqual(plan.payload_directory.name, "Zvec-Desktop")
        self.assertTrue(plan.portable_output.name.endswith("-portable.zip"))
        self.assertEqual(plan.nsis_path.name, "Zvec.PythonDesktop.nsi")
        self.assertEqual(plan.icon_path, repository_root() / "assets/Zvec.AppIcon.ico")
        self.assertNotIn("desktop/Zvec.Desktop", plan.icon_path.as_posix())
        self.assertIn("CPython 3.12", json.dumps(plan.to_dict()))
        self.assertIsNone(plan.makensis_command)

    def test_dry_run_succeeds_without_pyinstaller(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = build_python_desktop.main(["--dry-run"])
        self.assertEqual(exit_code, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["target_runtime"], "win-x64")
        self.assertEqual(plan["entry_points"], list(ENTRY_POINTS))
        self.assertEqual(plan["product"], "Zvec Desktop")

    def test_legacy_preview_entry_delegates_to_official_builder(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = build_python_preview.main(["--dry-run"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["product"], "Zvec Desktop")

    def test_windows_file_version_is_four_numeric_parts(self) -> None:
        self.assertEqual(windows_file_version("0.4.0"), "0.4.0.0")
        self.assertEqual(windows_file_version("1.2.3-preview.4"), "1.2.3.4")
        with self.assertRaises(PreviewPackagingError):
            windows_file_version("1.2")

    def test_spec_has_one_shared_analysis_and_required_exclusions(self) -> None:
        spec = (
            repository_root()
            / "release"
            / "python_preview"
            / "zvec_python_preview.spec"
        ).read_text(encoding="utf-8")
        self.assertEqual(spec.count("Analysis("), 1)
        self.assertEqual(spec.count("COLLECT("), 1)
        for executable in ("Zvec.Desktop", "zvec", "zvec-backend"):
            self.assertIn(f'name="{executable}"', spec)
        for excluded in ("openai", "torch", "transformers"):
            self.assertIn(f'"{excluded}"', spec)
        self.assertIn("Zvec.AppIcon.ico", spec)
        self.assertIn('(str(ICON), "assets")', spec)
        self.assertIn("hooks", spec)
        self.assertIn('name="Zvec-Desktop"', spec)

    def test_official_nsis_has_no_shell_or_wpf_dependency(self) -> None:
        nsis = (repository_root() / "installer" / "Zvec.PythonDesktop.nsi").read_text(
            encoding="utf-8"
        )
        folded = nsis.casefold()
        self.assertNotIn("powershell", folded)
        self.assertNotIn(".ps1", folded)
        self.assertNotIn("zvec.desktop.dll", folded)
        self.assertIn('!define product_name "zvec desktop"', folded)
        self.assertIn("zvec-backend.exe", folded)
        self.assertIn("$localappdata\\programs\\zvec desktop", folded)
        self.assertIn("..\\assets\\zvec.appicon.ico", folded)
        self.assertIn("{68cda461-fa06-44dc-a735-2a9c42f46d13}", folded)
        self.assertIn("launch.lock", folded)
        self.assertIn("backend.lock", folded)
        self.assertIn("createfilew", folded)
        self.assertIn("lockfile", folded)
        self.assertIn("seterrorlevel 35", folded)
        self.assertIn("seterrorlevel 36", folded)

    @unittest.skipUnless(
        os.getenv("ZVEC_MAKENSIS_PATH"),
        "ZVEC_MAKENSIS_PATH is not configured",
    )
    def test_official_nsis_compiles_with_synthetic_payload(self) -> None:
        makensis = Path(os.environ["ZVEC_MAKENSIS_PATH"])
        self.assertTrue(makensis.is_file(), makensis)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _valid_payload(root)
            output = root / "desktop-setup.exe"
            command = [
                str(makensis),
                "/DVERSION=0.4.0",
                "/DFILE_VERSION=0.4.0.0",
                f"/DSOURCE_DIR={payload}",
                f"/DOUTPUT_FILE={output}",
                "/DRID=win-x64",
                "/DSIGNING_STATUS=unsigned",
                str(repository_root() / "installer" / "Zvec.PythonDesktop.nsi"),
            ]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)

    @unittest.skipUnless(
        os.name == "nt" and os.getenv("ZVEC_MAKENSIS_PATH"),
        "Windows NSIS is required",
    )
    def test_installer_refuses_legacy_launch_and_backend_locks(self) -> None:
        makensis = Path(os.environ["ZVEC_MAKENSIS_PATH"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _valid_payload(root)
            output = root / "desktop-lock-guard-setup.exe"
            install_dir = root / "isolated-install"
            command = [
                str(makensis),
                "/DVERSION=0.4.0",
                "/DFILE_VERSION=0.4.0.0",
                f"/DSOURCE_DIR={payload}",
                f"/DOUTPUT_FILE={output}",
                "/DRID=win-x64",
                "/DSIGNING_STATUS=unsigned",
                "/DSKIP_PROCESS_GUARDS=1",
                f"/DINSTALL_DIR={install_dir}",
                "/DPRODUCT_REG_KEY=Software\\Zvec\\DesktopContractTest",
                (
                    "/DPRODUCT_UNINSTALL_KEY=Software\\Microsoft\\Windows\\"
                    "CurrentVersion\\Uninstall\\ZvecDesktopContractTest"
                ),
                f"/DSTART_MENU_SHORTCUT={root / 'unused-shortcut.lnk'}",
                str(repository_root() / "installer" / "Zvec.PythonDesktop.nsi"),
            ]
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            config_home = root / "config"
            backend_home = config_home / "backend"
            backend_home.mkdir(parents=True)
            environment = os.environ.copy()
            environment["ZVEC_CONFIG_HOME"] = str(config_home)

            with _exclusive_launch_lock(backend_home / "launch.lock"):
                _run_installer(
                    output,
                    environment=environment,
                    expected_exit_code=35,
                )
            self.assertFalse(install_dir.exists())

            with _backend_byte_lock(backend_home / "backend.lock"):
                _run_installer(
                    output,
                    environment=environment,
                    expected_exit_code=35,
                )
            self.assertFalse(install_dir.exists())


class FrozenEntryTest(unittest.TestCase):
    @staticmethod
    def _module(
        name: str,
        return_value: int,
        calls: list[list[str]],
    ) -> types.ModuleType:
        module = types.ModuleType(name)

        def main(arguments: list[str]) -> int:
            calls.append(arguments)
            return return_value

        module.main = main  # type: ignore[attr-defined]
        return module

    def test_three_executable_names_dispatch_to_python_entry_points(self) -> None:
        cases = (
            ("Zvec.Desktop.exe", "zvec_desktop.app", ["--folder", "x"], 10),
            ("zvec.exe", "zvec_launcher", ["stats"], 20),
            ("zvec-backend.exe", "image_service", ["serve", "--port", "1"], 30),
        )
        for executable, module_name, arguments, expected in cases:
            with self.subTest(executable=executable):
                calls: list[list[str]] = []
                module = self._module(module_name, expected, calls)
                with patch.dict(sys.modules, {module_name: module}):
                    actual = frozen_entry.main(arguments, executable=executable)
                self.assertEqual(actual, expected)
                self.assertEqual(calls, [arguments])

    def test_backend_defaults_to_serve(self) -> None:
        calls: list[list[str]] = []
        module = self._module("image_service", 0, calls)
        with patch.dict(sys.modules, {"image_service": module}):
            self.assertEqual(
                frozen_entry.main([], executable="zvec-backend.exe"),
                0,
            )
        self.assertEqual(calls, [["serve"]])

    def test_transitional_python_module_contract_is_supported(self) -> None:
        calls: list[list[str]] = []
        module = self._module("image_service", 7, calls)
        with patch.dict(sys.modules, {"image_service": module}):
            result = frozen_entry.main(
                ["-m", "image_service", "serve", "--port", "1234"],
                executable="Zvec.Desktop.exe",
            )
        self.assertEqual(result, 7)
        self.assertEqual(calls, [["serve", "--port", "1234"]])

    def test_unknown_executable_is_rejected(self) -> None:
        with self.assertRaises(frozen_entry.FrozenEntryError):
            frozen_entry.main([], executable="unexpected.exe")


if __name__ == "__main__":
    unittest.main()
