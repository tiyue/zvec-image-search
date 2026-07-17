from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import zvec_launcher
from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import EmbeddingResponse
from image_vector_service.service import ImageVectorService
from zvec_launcher import (
    LauncherError,
    LegacyDockerVolumeRequired,
    NativeLibrary,
    load_config,
    repair_and_verify_workspace,
    validate_native_config,
)


class FakeEmbeddingClient:
    def __init__(self, dimension: int):
        self.dimension = dimension
        self.request_count = 0

    def embed_images(self, image_paths: list[Path]) -> EmbeddingResponse:
        self.request_count += 1
        vectors = [
            [1.0, 0.01, *([0.0] * (self.dimension - 2))] for _path in image_paths
        ]
        return EmbeddingResponse(
            vectors=vectors,
            request_id=f"fake-{self.request_count}",
            usage={"images": len(image_paths)},
        )


class NativeLauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zvec_native_launcher_test_"))
        self.config_home = self.root / "config"
        self.images = self.root / "images"
        self.workspace = self.root / "workspace"
        self.results = self.root / "results"
        self.images.mkdir()
        self.environment = patch.dict(
            os.environ,
            {
                "ZVEC_CONFIG_HOME": str(self.config_home),
                "ZVEC_NATIVE_USE_SOURCE": "1",
            },
            clear=False,
        )
        self.environment.start()
        os.environ.pop("ZVEC_DOCKER_CONFIG_HOME", None)
        os.environ.pop("ZVEC_LIBRARY_ID", None)

    def tearDown(self) -> None:
        self.environment.stop()
        gc.collect()
        shutil.rmtree(self.root, ignore_errors=True)

    def write_config(self, payload: dict) -> Path:
        self.config_home.mkdir(parents=True, exist_ok=True)
        path = self.config_home / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_fake_python_command(
        self,
        directory: Path,
        name: str,
        architecture: str,
        selected_marker: Path,
    ) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        command = directory / f"{name}.cmd"
        probe_flag = directory / f"{name}-probe.flag"
        command.write_text(
            "@echo off\n"
            'if /I "%~1"=="-c" goto python_probe\n'
            'if /I "%~1"=="-m" if /I "%~2"=="pip" goto pip_probe\n'
            'if /I "%~1"=="-m" if /I "%~2"=="zvec_launcher" goto selected\n'
            "exit /b 0\n"
            ":python_probe\n"
            f'if exist "{probe_flag}" goto architecture_probe\n'
            f'type nul > "{probe_flag}"\n'
            "exit /b 0\n"
            ":architecture_probe\n"
            f'del /q "{probe_flag}" >nul 2>nul\n'
            f"echo Windows^|{architecture}\n"
            "exit /b 0\n"
            ":pip_probe\n"
            "echo pip 25.0 from fake runtime\n"
            "exit /b 0\n"
            ":selected\n"
            f'echo selected > "{selected_marker}"\n'
            "exit /b 0\n",
            encoding="utf-8",
        )
        return command

    def legacy_payload(self, workspace_type: str, workspace_source: str) -> dict:
        return {
            "schema_version": 2,
            "image_name": "zvec-image-search:legacy",
            "python_executable": "custom-python.exe",
            "results_directory": str(self.results.resolve()),
            "default_library_id": "library-main",
            "libraries": [
                {
                    "id": "library-main",
                    "name": "Main",
                    "image_root": str(self.images.resolve()),
                    "workspace_type": workspace_type,
                    "workspace_source": workspace_source,
                    "enabled": True,
                }
            ],
        }

    def test_init_writes_native_schema_and_forwards_host_paths(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = zvec_launcher.main(
                [
                    "init",
                    str(self.images),
                    "--workspace",
                    str(self.workspace),
                    "--results",
                    str(self.results),
                    "--skip-key",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads((self.config_home / "config.json").read_text())
        self.assertEqual(payload["schema_version"], 3)
        self.assertNotIn("python_executable", payload)
        self.assertNotIn("image_name", payload)
        self.assertNotIn("workspace_type", payload["libraries"][0])
        self.assertNotIn("workspace_source", payload["libraries"][0])
        self.assertEqual(
            payload["libraries"][0]["workspace_directory"],
            str(self.workspace.resolve()),
        )

        query = self.root / "query image.jpg"
        query.write_bytes(b"not-opened-by-routing-test")
        with patch.object(zvec_launcher, "_run_core", return_value=23) as run_core:
            code = zvec_launcher.main(["search-image", str(query), "--tk", "4"])
        self.assertEqual(code, 23)
        forwarded = run_core.call_args.args[2]
        self.assertEqual(forwarded[:3], ["search", "--image", str(query.resolve())])
        self.assertEqual(forwarded[3:], ["--tk", "4"])

    def test_legacy_command_experience_routes_to_native_core(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(
                zvec_launcher.main(
                    [
                        "init",
                        str(self.images),
                        "--workspace",
                        str(self.workspace),
                        "--results",
                        str(self.results),
                        "--skip-key",
                    ]
                ),
                0,
            )
        query = self.root / "query.jpg"
        query.write_bytes(b"routing-only")
        cases = (
            (["index", "new", "featured"], ["index", str(self.images), "new"]),
            (["sync", "--dry-run"], ["sync", str(self.images), "--dry-run"]),
            (["search", "sunset", "--tk", "3"], ["search", "--text", "sunset"]),
            (
                ["search-image", str(query), "--tk", "2"],
                ["search", "--image", str(query.resolve())],
            ),
            (
                ["search-mix", str(query), "portrait", "--tk", "5"],
                ["search", "--image", str(query.resolve()), "--text", "portrait"],
            ),
            (["stats"], ["stats"]),
            (["roots"], ["roots"]),
            (["cache-clear"], ["cache-clear"]),
            (["clean", "14", "--dry-run"], ["clean-results", "--days", "14"]),
            (["migrate-schema", "--dry-run"], ["migrate-schema", "--dry-run"]),
            (["raw", "serve", "--port", "9000"], ["serve", "--port", "9000"]),
        )
        for command, expected_prefix in cases:
            with (
                self.subTest(command=command),
                patch.object(zvec_launcher, "_run_core", return_value=0) as run_core,
            ):
                self.assertEqual(zvec_launcher.main(command), 0)
                forwarded = run_core.call_args.args[2]
                self.assertEqual(forwarded[: len(expected_prefix)], expected_prefix)

        # rebind-root keeps the stable root id but forwards a real host path.
        with patch.object(zvec_launcher, "_run_core", return_value=0) as run_core:
            self.assertEqual(zvec_launcher.main(["rebind-root", "root-1"]), 0)
        self.assertEqual(
            run_core.call_args.args[2],
            ["rebind-root", "root-1", str(self.images.resolve())],
        )

    def test_legacy_bind_workspace_is_migrated_without_docker(self) -> None:
        self.workspace.mkdir()
        path = self.write_config(
            self.legacy_payload("bind", str(self.workspace.resolve()))
        )
        with redirect_stderr(StringIO()):
            config = load_config()
        assert config is not None
        self.assertEqual(config.libraries[0].workspace_directory, self.workspace)
        migrated = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["python_executable"], "custom-python.exe")
        self.assertTrue((self.config_home / "config.v2.docker.backup.json").is_file())
        manifests = list(
            (self.config_home / "migration-backups").glob(
                "*/migration-backup-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 1)
        self.assertEqual(json.loads(manifests[0].read_text())["api_requests"], 0)

    def test_schema_v1_bind_migration_creates_manifest_before_v3_write(self) -> None:
        self.workspace.mkdir()
        path = self.write_config(
            {
                "schema_version": 1,
                "image_name": "zvec-image-search:legacy",
                "image_root": str(self.images.resolve()),
                "workspace_type": "bind",
                "workspace_source": str(self.workspace.resolve()),
                "results_directory": str(self.results.resolve()),
            }
        )
        with redirect_stderr(StringIO()):
            config = load_config()
        assert config is not None
        self.assertEqual(json.loads(path.read_text())["schema_version"], 3)
        self.assertTrue((self.config_home / "config.v1.docker.backup.json").is_file())
        manifests = list(
            (self.config_home / "migration-backups").glob(
                "*/migration-backup-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 1)

    def test_python_executable_survives_schema_v3_cli_mutations(self) -> None:
        second_images = self.root / "second-images"
        second_workspace = self.root / "second-workspace"
        moved_images = self.root / "moved-images"
        updated_results = self.root / "updated-results"
        for path in (
            self.workspace,
            second_images,
            second_workspace,
            moved_images,
        ):
            path.mkdir()
        self.write_config(
            {
                "schema_version": 3,
                "python_executable": "  custom-python.exe  ",
                "results_directory": str(self.results.resolve()),
                "default_library_id": "library-main",
                "libraries": [
                    {
                        "id": "library-main",
                        "name": "Main",
                        "image_root": str(self.images.resolve()),
                        "workspace_directory": str(self.workspace.resolve()),
                        "enabled": True,
                    },
                    {
                        "id": "library-second",
                        "name": "Second",
                        "image_root": str(second_images.resolve()),
                        "workspace_directory": str(second_workspace.resolve()),
                        "enabled": True,
                    },
                ],
            }
        )

        def assert_preserved() -> None:
            payload = json.loads(
                (self.config_home / "config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["python_executable"], "custom-python.exe")

        with redirect_stdout(StringIO()):
            self.assertEqual(
                zvec_launcher.main(["library-rename", "library-main", "Renamed main"]),
                0,
            )
        assert_preserved()

        with (
            patch.object(zvec_launcher, "_run_core", return_value=0),
            redirect_stdout(StringIO()),
        ):
            self.assertEqual(
                zvec_launcher.main(
                    ["rebind-root", "root-1", str(moved_images.resolve())]
                ),
                0,
            )
        assert_preserved()

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(
                zvec_launcher.main(
                    [
                        "init",
                        str(moved_images.resolve()),
                        "--workspace",
                        str(self.workspace.resolve()),
                        "--results",
                        str(updated_results.resolve()),
                        "--skip-key",
                    ]
                ),
                0,
            )
        assert_preserved()

        with redirect_stdout(StringIO()):
            self.assertEqual(
                zvec_launcher.main(["library-default", "library-second"]), 0
            )
            self.assertEqual(zvec_launcher.main(["library-remove", "library-main"]), 0)
        assert_preserved()

    def test_powershell_bootstrap_is_locked_and_fingerprints_all_package_code(
        self,
    ) -> None:
        script = Path(__file__).parents[1] / "scripts" / "zvec.ps1"
        text = script.read_text(encoding="utf-8")
        for required in (
            'Join-Path $sourceRoot "requirements-lock.txt"',
            'Join-Path $sourceRoot "model-catalog.default.json"',
            'Join-Path $sourceRoot "README.md"',
            'Join-Path $sourceRoot "zvec_logging.py"',
            '-Filter "*.py" -File -Recurse',
            '"--constraint", $requirementsLock',
            "--only-binary=:all:",
            "$runtimePython = (Get-NativeRuntimePaths).Python",
            "import zvec",
            "import PIL",
            "import numpy",
            "Get-PythonArchitectureState",
            "Get-Command $name -All",
            "Throw-WindowsArm64PythonUnsupported",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        resolve_start = text.index("function Resolve-PythonCommand")
        self.assertLess(
            text.index(
                "$runtimePython = (Get-NativeRuntimePaths).Python", resolve_start
            ),
            text.index('foreach ($name in @("python", "python3"))', resolve_start),
        )
        self.assertLess(
            text.index('if ($Command -ieq "runtime-doctor")'),
            text.index("$bootstrapPython = Resolve-PythonCommand"),
        )

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_install_failures_have_recoverable_classification(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        probe_script = self.root / "runtime-failure-classification.ps1"
        probe_script.write_text(
            "param([Parameter(Mandatory=$true)][string]$Launcher)\n"
            "$ErrorActionPreference = 'Stop'\n"
            "$tokens = $null; $errors = $null\n"
            "$ast = [Management.Automation.Language.Parser]::ParseFile(\n"
            "  $Launcher, [ref]$tokens, [ref]$errors)\n"
            "if ($errors.Count -ne 0) { throw "
            "($errors -join [Environment]::NewLine) }\n"
            "$required = @(\n"
            "  'Throw-RuntimeError',\n"
            "  'Throw-RuntimeCommandFailure',\n"
            "  'Get-RuntimeFailureData'\n"
            ")\n"
            "foreach ($name in $required) {\n"
            "  $definition = $ast.FindAll({\n"
            "    param($node)\n"
            "    $node -is "
            "[Management.Automation.Language.FunctionDefinitionAst] -and\n"
            "      $node.Name -eq $name\n"
            "  }, $true) | Select-Object -First 1\n"
            '  if ($null -eq $definition) { throw "Missing function: $name" }\n'
            "  Invoke-Expression $definition.Extent.Text\n"
            "}\n"
            "function Assert-Classification {\n"
            "  param([string]$Output, [string]$Code, [string]$ActionFragment)\n"
            "  try {\n"
            "    Throw-RuntimeCommandFailure -Output @($Output) `\n"
            "      -DefaultCode 'default' -DefaultMessage 'default' `\n"
            "      -DefaultRecommendedAction 'default'\n"
            "    throw 'Classification did not throw.'\n"
            "  } catch {\n"
            "    $actualCode = [string]$_.Exception.Data['ZvecRuntimeCode']\n"
            "    $actualAction = [string]$_.Exception.Data['ZvecRecommendedAction']\n"
            "    if ($actualCode -ne $Code -or "
            "$actualAction -notmatch $ActionFragment) {\n"
            '      throw "Unexpected classification: $actualCode | $actualAction"\n'
            "    }\n"
            "  }\n"
            "}\n"
            "Assert-Classification 'OSError: [Errno 28] No space left on device' `\n"
            "  'runtime_disk_space_insufficient' '1 GB'\n"
            "Assert-Classification 'NewConnectionError: getaddrinfo failed' `\n"
            "  'runtime_network_unavailable' '已有图库和索引不会被修改'\n"
            "Assert-Classification '' 'default' '^default$'\n"
            "try { throw [IO.IOException]::new('No space left on device') } catch {\n"
            "  $fallback = Get-RuntimeFailureData -ErrorRecord $_\n"
            "  if ($fallback.Code -ne 'runtime_disk_space_insufficient' -or `\n"
            "      $fallback.Action -notmatch '1 GB') {\n"
            '    throw "Unexpected filesystem fallback: $($fallback.Code)"\n'
            "  }\n"
            "}\n",
            encoding="utf-8-sig",
        )
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(probe_script),
                str(Path(__file__).parents[1] / "scripts" / "zvec.ps1"),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_python_resolution_skips_arm64_and_selects_later_x64(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        arm_marker = self.root / "arm-selected.txt"
        x64_marker = self.root / "x64-selected.txt"
        arm_python = self.write_fake_python_command(
            self.root / "arm-python",
            "python",
            "ARM64",
            arm_marker,
        )
        self.write_fake_python_command(
            self.root / "x64-python",
            "python",
            "AMD64",
            x64_marker,
        )
        script = Path(__file__).parents[1] / "scripts" / "zvec.ps1"
        environment = os.environ.copy()
        environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
        environment["ZVEC_NATIVE_USE_SOURCE"] = "1"
        environment["ZVEC_UTF8_OUTPUT"] = "1"
        environment["PATH"] = os.pathsep.join(
            [str(arm_python.parent), str(self.root / "x64-python")]
        )
        environment.pop("ZVEC_PYTHON", None)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "help",
            ],
            cwd=Path(__file__).parents[1],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(x64_marker.is_file())
        self.assertFalse(arm_marker.exists())

        only_arm_environment = environment.copy()
        only_arm_environment["PATH"] = str(arm_python.parent)
        only_arm_environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
        only_arm = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "runtime-bootstrap",
            ],
            cwd=Path(__file__).parents[1],
            env=only_arm_environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(only_arm.returncode, 1)
        self.assertIn(
            '"code":"windows_arm64_python_unsupported"',
            only_arm.stdout,
        )

        explicit_environment = environment.copy()
        explicit_environment["ZVEC_PYTHON"] = str(arm_python)
        explicit_environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
        explicit_arm = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "runtime-bootstrap",
            ],
            cwd=Path(__file__).parents[1],
            env=explicit_environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(explicit_arm.returncode, 1)
        self.assertIn(
            '"code":"windows_arm64_python_unsupported"',
            explicit_arm.stdout,
        )

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_bootstrap_rebuilds_arm64_venv_with_x64_python(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        package = self.root / "arm64-runtime-package"
        scripts = package / "scripts"
        backend = package / "backend"
        service = backend / "image_vector_service"
        scripts.mkdir(parents=True)
        service.mkdir(parents=True)
        shutil.copy2(Path(__file__).parents[1] / "scripts" / "zvec.ps1", scripts)
        sources = {
            "pyproject.toml": "[project\nname='arm64-runtime-smoke'\n",
            "requirements.txt": "",
            "requirements-lock.txt": "",
            "model-catalog.default.json": "{}\n",
            "README.md": "arm64 runtime smoke\n",
            "image_service.py": "# smoke\n",
            "zvec_launcher.py": "# smoke\n",
            "zvec_logging.py": "# smoke\n",
        }
        for name, content in sources.items():
            (backend / name).write_text(content, encoding="utf-8")
        (service / "__init__.py").write_text("# smoke\n", encoding="utf-8")

        venv = self.config_home / "runtime" / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        venv_python = venv / "Scripts" / "python.exe"
        site_packages = Path(
            subprocess.run(
                [
                    str(venv_python),
                    "-c",
                    "import site; print(site.getsitepackages()[0])",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        )
        sitecustomize = site_packages / "sitecustomize.py"
        sitecustomize.write_text(
            "import platform\n"
            "platform.system = lambda: 'Windows'\n"
            "platform.machine = lambda: 'ARM64'\n",
            encoding="utf-8",
        )
        simulated = subprocess.run(
            [
                str(venv_python),
                "-c",
                "import platform; print(platform.system() + '|' + platform.machine())",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(simulated.stdout.strip(), "Windows|ARM64")

        base_environment = os.environ.copy()
        base_environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
        base_environment["ZVEC_UTF8_OUTPUT"] = "1"
        base_environment.pop("ZVEC_PYTHON", None)
        base_environment.pop("ZVEC_NATIVE_USE_SOURCE", None)

        arm_only_environment = base_environment.copy()
        arm_only_environment["PATH"] = str(Path(powershell).parent)
        arm_only = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "zvec.ps1"),
                "runtime-bootstrap",
            ],
            cwd=package,
            env=arm_only_environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(arm_only.returncode, 1)
        self.assertIn(
            '"code":"windows_arm64_python_unsupported"',
            arm_only.stdout,
        )
        self.assertTrue(sitecustomize.is_file())

        recovery_environment = base_environment.copy()
        recovery_environment["PATH"] = str(Path(sys.executable).parent)
        recovered = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "zvec.ps1"),
                "runtime-bootstrap",
            ],
            cwd=package,
            env=recovery_environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(recovered.returncode, 1)
        self.assertIn('"code":"wheel_build_failed"', recovered.stdout)
        self.assertFalse(sitecustomize.exists())
        rebuilt = subprocess.run(
            [
                str(venv_python),
                "-c",
                "import platform; print(platform.system() + '|' + platform.machine())",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotIn("ARM64", rebuilt.stdout)

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_doctor_uses_existing_venv_without_system_python(self) -> None:
        package = self.root / "runtime-package"
        scripts = package / "scripts"
        backend = package / "backend"
        service = backend / "image_vector_service"
        scripts.mkdir(parents=True)
        service.mkdir(parents=True)
        shutil.copy2(Path(__file__).parents[1] / "scripts" / "zvec.ps1", scripts)
        required_sources = {
            "pyproject.toml": (
                "[project]\nname='runtime-doctor-smoke'\nversion='0.0.0'\n"
            ),
            "requirements.txt": "",
            "requirements-lock.txt": "",
            "model-catalog.default.json": "{}\n",
            "README.md": "runtime doctor smoke\n",
            "image_service.py": "# smoke\n",
            "zvec_launcher.py": "# smoke\n",
            "zvec_logging.py": "# smoke\n",
        }
        for name, content in required_sources.items():
            (backend / name).write_text(content, encoding="utf-8")
        (service / "__init__.py").write_text("# smoke\n", encoding="utf-8")

        runtime_root = self.config_home / "runtime"
        venv = runtime_root / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        files = [
            backend / "pyproject.toml",
            backend / "requirements.txt",
            backend / "requirements-lock.txt",
            backend / "model-catalog.default.json",
            backend / "README.md",
            backend / "image_service.py",
            backend / "zvec_launcher.py",
            backend / "zvec_logging.py",
            *sorted(service.rglob("*.py")),
        ]
        fingerprint_input = "".join(
            f"{path.resolve()}:{hashlib.sha256(path.read_bytes()).hexdigest().upper()}\n"
            for path in files
        ).encode()
        (runtime_root / "source.sha256").write_text(
            hashlib.sha256(fingerprint_input).hexdigest() + "\n",
            encoding="utf-8",
        )

        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        environment = os.environ.copy()
        environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
        environment["PATH"] = str(Path(powershell).parent)
        environment["ZVEC_UTF8_OUTPUT"] = "1"
        environment.pop("ZVEC_PYTHON", None)
        environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "zvec.ps1"),
                "runtime-doctor",
            ],
            cwd=package,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"status":"ready"', result.stdout)
        self.assertIn('"name":"zvec"', result.stdout)

        environment["ZVEC_NATIVE_USE_SOURCE"] = "1"
        ordinary = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "zvec.ps1"),
                "help",
            ],
            cwd=package,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(ordinary.returncode, 0, ordinary.stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_doctor_marks_broken_venv_as_automatically_repairable(self) -> None:
        package = self.root / "broken-doctor-package"
        scripts = package / "scripts"
        backend = package / "backend"
        service = backend / "image_vector_service"
        scripts.mkdir(parents=True)
        service.mkdir(parents=True)
        shutil.copy2(Path(__file__).parents[1] / "scripts" / "zvec.ps1", scripts)
        required_sources = {
            "pyproject.toml": (
                "[project]\nname='broken-doctor-smoke'\nversion='0.0.0'\n"
            ),
            "requirements.txt": "",
            "requirements-lock.txt": "",
            "model-catalog.default.json": "{}\n",
            "README.md": "broken doctor smoke\n",
            "image_service.py": "# smoke\n",
            "zvec_launcher.py": "# smoke\n",
            "zvec_logging.py": "# smoke\n",
        }
        for name, content in required_sources.items():
            (backend / name).write_text(content, encoding="utf-8")
        (service / "__init__.py").write_text("# smoke\n", encoding="utf-8")
        files = [
            backend / "pyproject.toml",
            backend / "requirements.txt",
            backend / "requirements-lock.txt",
            backend / "model-catalog.default.json",
            backend / "README.md",
            backend / "image_service.py",
            backend / "zvec_launcher.py",
            backend / "zvec_logging.py",
            *sorted(service.rglob("*.py")),
        ]
        fingerprint_input = "".join(
            f"{path.resolve()}:{hashlib.sha256(path.read_bytes()).hexdigest().upper()}\n"
            for path in files
        ).encode()
        runtime_root = self.config_home / "runtime"
        broken_python = runtime_root / "venv" / "Scripts" / "python.exe"
        broken_python.parent.mkdir(parents=True)
        broken_python.write_bytes(b"broken")
        (runtime_root / "source.sha256").write_text(
            hashlib.sha256(fingerprint_input).hexdigest() + "\n",
            encoding="utf-8",
        )

        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        environment = os.environ.copy()
        environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
        environment["PATH"] = str(Path(powershell).parent)
        environment["ZVEC_UTF8_OUTPUT"] = "1"
        environment.pop("ZVEC_PYTHON", None)
        environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "zvec.ps1"),
                "runtime-doctor",
            ],
            cwd=package,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('"code":"venv_unusable"', result.stdout)
        self.assertIn("修复运行环境", result.stdout)

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_bootstrap_lock_timeout_is_structured(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        runtime_root = self.config_home / "runtime"
        runtime_root.mkdir(parents=True)
        lock_path = runtime_root / "bootstrap.lock"
        ready_path = self.root / "lock-ready.txt"
        holder_script = self.root / "hold-runtime-lock.ps1"
        holder_script.write_text(
            "param([string]$LockPath, [string]$ReadyPath)\n"
            "$stream = [IO.File]::Open("
            "$LockPath, 'OpenOrCreate', 'ReadWrite', 'None')\n"
            "try {\n"
            "  [IO.File]::WriteAllText($ReadyPath, 'ready')\n"
            "  Start-Sleep -Seconds 15\n"
            "} finally { $stream.Dispose() }\n",
            encoding="utf-8-sig",
        )
        holder = subprocess.Popen(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(holder_script),
                str(lock_path),
                str(ready_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(100):
                if ready_path.is_file():
                    break
                if holder.poll() is not None:
                    self.fail("runtime lock holder exited before becoming ready")
                time.sleep(0.05)
            self.assertTrue(ready_path.is_file(), "lock holder did not become ready")

            environment = os.environ.copy()
            environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
            environment["ZVEC_PYTHON"] = sys.executable
            environment["ZVEC_RUNTIME_LOCK_TIMEOUT_SECONDS"] = "1"
            environment["ZVEC_UTF8_OUTPUT"] = "1"
            environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
            result = subprocess.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(Path(__file__).parents[1] / "scripts" / "zvec.ps1"),
                    "runtime-bootstrap",
                ],
                cwd=Path(__file__).parents[1],
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn('"code":"runtime_lock_timeout"', result.stdout)
            self.assertIn("另一个窗口或进程正在准备", result.stdout)
            self.assertFalse((runtime_root / "venv").exists())
        finally:
            holder.terminate()
            try:
                holder.wait(timeout=5)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait(timeout=5)

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_bootstrap_rebuilds_broken_venv_with_system_python(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        package = self.root / "broken-runtime-package"
        scripts = package / "scripts"
        backend = package / "backend"
        service = backend / "image_vector_service"
        scripts.mkdir(parents=True)
        service.mkdir(parents=True)
        shutil.copy2(Path(__file__).parents[1] / "scripts" / "zvec.ps1", scripts)
        sources = {
            # Deliberately invalid metadata makes wheel building stop after venv
            # recovery;
            # the test therefore needs neither network access nor package installation.
            "pyproject.toml": "[project\nname='broken-runtime-smoke'\n",
            "requirements.txt": "",
            "requirements-lock.txt": "",
            "model-catalog.default.json": "{}\n",
            "README.md": "broken runtime smoke\n",
            "image_service.py": "# smoke\n",
            "zvec_launcher.py": "# smoke\n",
            "zvec_logging.py": "# smoke\n",
        }
        for name, content in sources.items():
            (backend / name).write_text(content, encoding="utf-8")
        (service / "__init__.py").write_text("# smoke\n", encoding="utf-8")

        broken_python = self.config_home / "runtime" / "venv" / "Scripts" / "python.exe"
        broken_python.parent.mkdir(parents=True)
        broken_python.write_bytes(b"not-a-python-executable")
        stale_wheel = self.config_home / "runtime" / f"wheel-{'a' * 32}"
        stale_wheel.mkdir()
        (stale_wheel / "partial.whl").write_bytes(b"partial")
        unrelated_wheel = self.config_home / "runtime" / "wheel-user-backup"
        unrelated_wheel.mkdir()
        (unrelated_wheel / "keep.txt").write_text("keep", encoding="utf-8")
        unrelated_directory = self.config_home / "runtime" / "user-data"
        unrelated_directory.mkdir()
        python_path = self.root / "python-path"
        python_path.mkdir()
        # Simulate the non-runnable Microsoft Store python.exe alias. Resolution must
        # continue to the next usable candidate instead of reporting a version error.
        (python_path / "python.exe").write_bytes(b"broken-store-alias")
        (python_path / "python3.cmd").write_text(
            f'@"{sys.executable}" %*\n',
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
        environment["PATH"] = str(python_path)
        environment["ZVEC_UTF8_OUTPUT"] = "1"
        environment.pop("ZVEC_PYTHON", None)
        environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "zvec.ps1"),
                "runtime-bootstrap",
            ],
            cwd=package,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('"code":"wheel_build_failed"', result.stdout, result.stderr)
        probe = subprocess.run(
            [str(broken_python), "-c", "import sys; print(sys.version_info[:2])"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertNotEqual(broken_python.read_bytes(), b"not-a-python-executable")
        self.assertFalse(stale_wheel.exists())
        self.assertTrue((unrelated_wheel / "keep.txt").is_file())
        self.assertTrue(unrelated_directory.is_dir())

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_bootstrap_replaces_half_installed_venv_directory(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        package = self.root / "partial-runtime-package"
        scripts = package / "scripts"
        backend = package / "backend"
        service = backend / "image_vector_service"
        scripts.mkdir(parents=True)
        service.mkdir(parents=True)
        shutil.copy2(Path(__file__).parents[1] / "scripts" / "zvec.ps1", scripts)
        sources = {
            # Stop after venv recovery without requiring network or dependency install.
            "pyproject.toml": "[project\nname='partial-runtime-smoke'\n",
            "requirements.txt": "",
            "requirements-lock.txt": "",
            "model-catalog.default.json": "{}\n",
            "README.md": "partial runtime smoke\n",
            "image_service.py": "# smoke\n",
            "zvec_launcher.py": "# smoke\n",
            "zvec_logging.py": "# smoke\n",
        }
        for name, content in sources.items():
            (backend / name).write_text(content, encoding="utf-8")
        (service / "__init__.py").write_text("# smoke\n", encoding="utf-8")

        runtime_root = self.config_home / "runtime"
        venv = runtime_root / "venv"
        partial_file = venv / "Lib" / "site-packages" / "partial-install.txt"
        partial_file.parent.mkdir(parents=True)
        partial_file.write_text("must be removed", encoding="utf-8")
        (runtime_root / "source.sha256").write_text(
            "stale-partial-install\n", encoding="utf-8"
        )

        environment = os.environ.copy()
        environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
        environment["ZVEC_PYTHON"] = sys.executable
        environment["ZVEC_UTF8_OUTPUT"] = "1"
        environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "zvec.ps1"),
                "runtime-bootstrap",
            ],
            cwd=package,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('"code":"wheel_build_failed"', result.stdout, result.stderr)
        self.assertFalse(partial_file.exists())
        self.assertFalse((runtime_root / "source.sha256").exists())
        rebuilt_python = venv / "Scripts" / "python.exe"
        probe = subprocess.run(
            [str(rebuilt_python), "-c", "import sys; print(sys.version_info[:2])"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_bootstrap_rebuilds_venv_when_pip_is_missing(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        package = self.root / "missing-pip-package"
        scripts = package / "scripts"
        backend = package / "backend"
        service = backend / "image_vector_service"
        scripts.mkdir(parents=True)
        service.mkdir(parents=True)
        shutil.copy2(Path(__file__).parents[1] / "scripts" / "zvec.ps1", scripts)
        sources = {
            "pyproject.toml": "[project\nname='missing-pip-smoke'\n",
            "requirements.txt": "",
            "requirements-lock.txt": "",
            "model-catalog.default.json": "{}\n",
            "README.md": "missing pip smoke\n",
            "image_service.py": "# smoke\n",
            "zvec_launcher.py": "# smoke\n",
            "zvec_logging.py": "# smoke\n",
        }
        for name, content in sources.items():
            (backend / name).write_text(content, encoding="utf-8")
        (service / "__init__.py").write_text("# smoke\n", encoding="utf-8")

        venv = self.config_home / "runtime" / "venv"
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        venv_python = venv / "Scripts" / "python.exe"
        subprocess.run(
            [str(venv_python), "-m", "pip", "uninstall", "--yes", "pip"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        missing_pip = subprocess.run(
            [str(venv_python), "-m", "pip", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(missing_pip.returncode, 0)

        python_path = self.root / "pip-recovery-python"
        python_path.mkdir()
        (python_path / "python.cmd").write_text(
            f'@"{sys.executable}" %*\n',
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
        environment["PATH"] = str(python_path)
        environment["ZVEC_UTF8_OUTPUT"] = "1"
        environment.pop("ZVEC_PYTHON", None)
        environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "zvec.ps1"),
                "runtime-bootstrap",
            ],
            cwd=package,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('"code":"wheel_build_failed"', result.stdout)
        repaired_pip = subprocess.run(
            [str(venv_python), "-m", "pip", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(repaired_pip.returncode, 0, repaired_pip.stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_bootstrap_reports_unwritable_config_without_secondary_error(
        self,
    ) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        blocking_file = self.root / "config-is-a-file"
        blocking_file.write_text("not a directory", encoding="utf-8")
        environment = os.environ.copy()
        environment["ZVEC_CONFIG_HOME"] = str(blocking_file / "child")
        environment["ZVEC_PYTHON"] = sys.executable
        environment["ZVEC_UTF8_OUTPUT"] = "1"
        environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(Path(__file__).parents[1] / "scripts" / "zvec.ps1"),
                "runtime-bootstrap",
            ],
            cwd=Path(__file__).parents[1],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        result_lines = [
            line
            for line in result.stdout.splitlines()
            if line.startswith("@@ZVEC_RUNTIME_RESULT@@")
        ]
        self.assertEqual(len(result_lines), 1, result.stdout)
        self.assertIn('"code":"config_directory_unavailable"', result_lines[0])
        self.assertIn("ZVEC_CONFIG_HOME", result_lines[0])

    @unittest.skipUnless(os.name == "nt", "PowerShell runtime smoke is Windows-only")
    def test_runtime_bootstrap_reports_missing_python_with_action(self) -> None:
        powershell = shutil.which("powershell.exe")
        self.assertIsNotNone(powershell)
        environment = os.environ.copy()
        environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
        environment["PATH"] = str(Path(powershell).parent)
        environment["ZVEC_UTF8_OUTPUT"] = "1"
        environment.pop("ZVEC_PYTHON", None)
        environment.pop("ZVEC_NATIVE_USE_SOURCE", None)
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(Path(__file__).parents[1] / "scripts" / "zvec.ps1"),
                "runtime-bootstrap",
            ],
            cwd=Path(__file__).parents[1],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn('"code":"python_not_found"', result.stdout)
        self.assertIn("python.org", result.stdout)
        self.assertIn("App Execution Alias", result.stdout)

    @unittest.skipUnless(os.name == "nt", "PowerShell packaging smoke is Windows-only")
    def test_powershell_bootstrap_resolves_packaged_backend_root(self) -> None:
        package = self.root / "package"
        scripts = package / "scripts"
        backend = package / "backend"
        scripts.mkdir(parents=True)
        backend.mkdir()
        shutil.copy2(Path(__file__).parents[1] / "scripts" / "zvec.ps1", scripts)
        (backend / "pyproject.toml").write_text(
            "[project]\nname='packaged-backend-smoke'\nversion='0.0.0'\n",
            encoding="utf-8",
        )
        (backend / "zvec_launcher.py").write_text(
            "print('PACKAGED_BACKEND_ROOT_OK')\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["ZVEC_NATIVE_USE_SOURCE"] = "1"
        environment["ZVEC_CONFIG_HOME"] = str(self.config_home)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "zvec.ps1"),
                "help",
            ],
            cwd=self.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PACKAGED_BACKEND_ROOT_OK", result.stdout)

    def test_legacy_bind_repair_failure_keeps_recoverable_v2_config(self) -> None:
        self.workspace.mkdir()
        path = self.write_config(
            self.legacy_payload("bind", str(self.workspace.resolve()))
        )
        with (
            patch.object(
                zvec_launcher,
                "repair_and_verify_workspace",
                side_effect=LauncherError("repair failed"),
            ),
            self.assertRaisesRegex(LauncherError, "repair failed"),
        ):
            load_config()
        self.assertEqual(json.loads(path.read_text())["schema_version"], 2)
        backup = self.config_home / "config.v2.docker.backup.json"
        self.assertTrue(backup.is_file())
        self.assertEqual(json.loads(backup.read_text())["schema_version"], 2)

    def test_named_volume_requires_explicit_export(self) -> None:
        self.write_config(
            self.legacy_payload("volume", "zvec-image-workspace-existing")
        )
        with self.assertRaises(LegacyDockerVolumeRequired):
            load_config()

    def test_volume_export_rejects_docker_image_option_injection(self) -> None:
        destination = self.root / "malicious-export"
        with (
            patch.object(zvec_launcher, "_run_checked") as run_checked,
            self.assertRaisesRegex(LauncherError, "Invalid Docker image repository"),
        ):
            zvec_launcher.export_docker_volume(
                "zvec-image-workspace-existing",
                "--privileged",
                destination,
            )
        run_checked.assert_not_called()
        self.assertFalse(destination.exists())

    def test_volume_migration_rejects_library_id_path_traversal_before_export(
        self,
    ) -> None:
        payload = self.legacy_payload("volume", "zvec-image-workspace-existing")
        payload["default_library_id"] = "../../../outside"
        payload["libraries"][0]["id"] = "../../../outside"
        self.write_config(payload)
        with (
            patch.object(zvec_launcher, "export_docker_volume") as export,
            redirect_stderr(StringIO()),
        ):
            code = zvec_launcher.main(["migrate-docker-workspace"])
        self.assertEqual(code, 1)
        export.assert_not_called()
        self.assertFalse((self.root / "outside").exists())

    def test_volume_export_is_staged_and_bound_to_source_volume(self) -> None:
        destination = self.root / "safe-export"
        commands: list[list[str]] = []

        def fake_run_checked(
            command: list[str] | tuple[str, ...], _description: str
        ) -> subprocess.CompletedProcess[str]:
            values = list(command)
            commands.append(values)
            if values[1] == "cp":
                staging = Path(values[-1])
                (staging / "image_collection").mkdir()
                (staging / "image_collection.meta.json").write_text(
                    "{}", encoding="utf-8"
                )
                (staging / "image_collection.state.sqlite3").write_bytes(b"state")
            return subprocess.CompletedProcess(values, 0, "", "")

        with (
            patch.object(zvec_launcher, "_run_checked", fake_run_checked),
            patch.object(subprocess, "run") as cleanup,
        ):
            report = zvec_launcher.export_docker_volume(
                "zvec-image-workspace-existing",
                "registry.example.com:5000/zvec/image-search:legacy",
                destination,
            )

        self.assertEqual(report["status"], "exported")
        create = commands[0]
        separator = create.index("--")
        self.assertEqual(
            create[separator + 1],
            "registry.example.com:5000/zvec/image-search:legacy",
        )
        self.assertTrue((destination / "image_collection").is_dir())
        receipt = json.loads(
            (destination / zvec_launcher.DOCKER_EXPORT_RECEIPT).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["status"], "complete")
        self.assertEqual(receipt["volume"], "zvec-image-workspace-existing")
        cleanup.assert_called_once()

        with self.assertRaisesRegex(LauncherError, "does not match Docker volume"):
            zvec_launcher.export_docker_volume(
                "different-volume",
                "zvec-image-search:legacy",
                destination,
            )

    def test_partial_export_without_receipt_is_never_reused(self) -> None:
        destination = self.root / "partial-export"
        (destination / "image_collection").mkdir(parents=True)
        (destination / "image_collection.meta.json").write_text("{}", encoding="utf-8")
        (destination / "image_collection.state.sqlite3").write_bytes(b"state")
        with self.assertRaisesRegex(LauncherError, "no verified export receipt"):
            zvec_launcher.export_docker_volume(
                "zvec-image-workspace-existing",
                "zvec-image-search:legacy",
                destination,
            )

    def test_native_config_rejects_runtime_data_inside_image_root(self) -> None:
        with self.assertRaisesRegex(LauncherError, "Results directory cannot overlap"):
            validate_native_config(
                {
                    "schema_version": 3,
                    "results_directory": str(self.images / "generated-results"),
                    "default_library_id": "library-main",
                    "libraries": [
                        {
                            "id": "library-main",
                            "name": "Main",
                            "image_root": str(self.images),
                            "workspace_directory": str(self.workspace),
                            "enabled": True,
                        }
                    ],
                }
            )

    def test_workspace_backup_command_supports_dry_run_and_creation(self) -> None:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(
                zvec_launcher.main(
                    [
                        "init",
                        str(self.images),
                        "--workspace",
                        str(self.workspace),
                        "--results",
                        str(self.results),
                        "--skip-key",
                    ]
                ),
                0,
            )
        destination = self.root / "manual-backup"
        output = StringIO()
        with redirect_stdout(output):
            code = zvec_launcher.main(
                [
                    "workspace-backup",
                    "--destination",
                    str(destination),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual(plan["status"], "planned")
        self.assertEqual(plan["plan"]["status"], "ready")
        self.assertFalse(destination.exists())

        output = StringIO()
        with redirect_stdout(output):
            code = zvec_launcher.main(
                ["workspace-backup", "--destination", str(destination)]
            )
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["status"], "created")
        self.assertEqual(report["api_requests"], 0)
        self.assertTrue((destination / "migration-backup-manifest.json").is_file())

    def test_explicit_volume_migration_writes_v3_after_verification(self) -> None:
        self.write_config(
            self.legacy_payload("volume", "zvec-image-workspace-existing")
        )
        destination = self.root / "exported-workspace"

        def fake_export(volume: str, image: str, output: Path) -> dict:
            output.mkdir(parents=True)
            return {
                "status": "exported",
                "volume": volume,
                "image": image,
                "destination": str(output),
            }

        with (
            patch.object(zvec_launcher, "export_docker_volume", fake_export),
            patch.object(
                zvec_launcher,
                "repair_and_verify_workspace",
                return_value={"status": "no_index", "api_requests": 0},
            ),
            redirect_stdout(StringIO()),
        ):
            code = zvec_launcher.main(
                [
                    "migrate-docker-workspace",
                    "--destination",
                    str(destination),
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads((self.config_home / "config.json").read_text())
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(
            payload["libraries"][0]["workspace_directory"],
            str(destination.resolve()),
        )
        manifests = list(
            (self.config_home / "migration-backups").glob(
                "*/migration-backup-manifest.json"
            )
        )
        self.assertEqual(len(manifests), 1)

    def test_failed_volume_repair_still_preserves_legacy_config_backup(self) -> None:
        path = self.write_config(
            self.legacy_payload("volume", "zvec-image-workspace-existing")
        )
        destination = self.root / "failed-export"

        def fake_export(volume: str, image: str, output: Path) -> dict:
            output.mkdir(parents=True)
            return {
                "status": "exported",
                "volume": volume,
                "image": image,
                "destination": str(output),
            }

        with (
            patch.object(zvec_launcher, "export_docker_volume", fake_export),
            patch.object(
                zvec_launcher,
                "repair_and_verify_workspace",
                side_effect=LauncherError("verification failed"),
            ),
            redirect_stderr(StringIO()),
        ):
            code = zvec_launcher.main(
                [
                    "migrate-docker-workspace",
                    "--destination",
                    str(destination),
                ]
            )
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(path.read_text())["schema_version"], 2)
        backup = self.config_home / "config.v2.docker.backup.json"
        self.assertTrue(backup.is_file())
        self.assertEqual(json.loads(backup.read_text())["schema_version"], 2)

    def test_existing_vectors_are_verified_without_an_api_request(self) -> None:
        Image.new("RGB", (16, 16), (255, 0, 0)).save(self.images / "red.png")
        config = ServiceConfig(
            workspace=self.workspace,
            results_directory=self.results,
        )
        client = FakeEmbeddingClient(config.dimension)
        service = ImageVectorService(config=config, embedding_client=client)
        try:
            report = service.index_folder(str(self.images))
            self.assertEqual(report.inserted, 1)
            self.assertEqual(client.request_count, 1)
        finally:
            service.close()
            del service
            gc.collect()

        library = NativeLibrary(
            "library-main",
            "Main",
            self.images.resolve(),
            self.workspace.resolve(),
        )
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}, clear=False):
            verification = repair_and_verify_workspace(
                library,
                self.results.resolve(),
                dry_run=False,
            )
        self.assertEqual(verification["api_requests"], 0)
        self.assertEqual(verification["collection_documents"], 1)
        self.assertEqual(verification["sqlite_entries"], 1)
        self.assertEqual(verification["search_probe"], "passed")

        self.write_config(
            {
                "schema_version": 3,
                "results_directory": str(self.results.resolve()),
                "default_library_id": "library-main",
                "libraries": [library.to_dict()],
            }
        )
        backup = self.root / "verified-migration-backup"
        output = StringIO()
        with redirect_stdout(output):
            code = zvec_launcher.main(
                [
                    "migrate-docker-workspace",
                    "--backup-directory",
                    str(backup),
                ]
            )
        self.assertEqual(code, 0)
        migration = json.loads(output.getvalue())
        self.assertEqual(migration["api_requests"], 0)
        self.assertEqual(migration["collection_documents"], 1)
        self.assertEqual(migration["sqlite_entries"], 1)
        self.assertEqual(migration["search_probe"], "passed")
        self.assertTrue((backup / "migration-backup-manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
