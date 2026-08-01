from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import call, patch

from release.webview_preview import frozen_entry, runtime_hook
from scripts import build_webview_preview
from scripts.webview_preview_packaging import (
    ENTRY_POINTS,
    PRODUCT_DIRECTORY,
    WebviewPreviewPackagingError,
    create_build_plan,
    inspect_payload,
    public_version,
    repository_root,
    target_host_errors,
    validate_frontend_output,
    validate_static_inputs,
    verify_payload_manifest,
    windows_file_version,
    write_payload_manifest,
    write_portable_archive,
)
from zvec_webview.resident_task import (
    MAIN_EXECUTABLE as RESIDENT_MAIN_EXECUTABLE,
)
from zvec_webview.resident_task import (
    PRODUCT_DIRECTORY as RESIDENT_PRODUCT_DIRECTORY,
)


def _write(root: Path, relative: str, content: bytes = b"preview") -> None:
    path = root / Path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _valid_payload(root: Path) -> Path:
    payload = root / PRODUCT_DIRECTORY
    for entry_point in ENTRY_POINTS:
        _write(payload, entry_point)
    for relative in (
        "_internal/python312.dll",
        "_internal/pythonnet/runtime/Python.Runtime.dll",
        "_internal/pythonnet/runtime/netstandard.dll",
        "_internal/pythonnet/runtime/System.Runtime.dll",
        "_internal/webview/lib/Microsoft.Web.WebView2.Core.dll",
        "_internal/webview/lib/Microsoft.Web.WebView2.WinForms.dll",
        "_internal/webview/lib/WebBrowserInterop.x64.dll",
        "_internal/webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
        "_internal/webview/js/api.js",
        "_internal/webview/js/finish.js",
        "_internal/PIL/_imaging.cp312-win_amd64.pyd",
        "_internal/zvec/_zvec.cp312-win_amd64.pyd",
        "_internal/model-catalog.default.json",
        "_internal/assets/Zvec.AppIcon.ico",
    ):
        _write(payload, relative)
    frontend = payload / "_internal/zvec_webview/frontend_dist"
    _write(
        frontend,
        "index.html",
        (
            b'<!doctype html><script type="module" '
            b'src="./assets/index-A1b2C3.js"></script>'
            b'<link rel="stylesheet" href="./assets/index-D4e5F6.css">'
        ),
    )
    _write(frontend, "assets/index-A1b2C3.js", b"window.zvecReady=true;")
    _write(frontend, "assets/index-D4e5F6.css", b"body{color:#111}")
    _write(
        frontend,
        ".vite/manifest.json",
        json.dumps(
            {
                "index.html": {
                    "file": "assets/index-A1b2C3.js",
                    "name": "index",
                    "src": "index.html",
                    "isEntry": True,
                    "css": ["assets/index-D4e5F6.css"],
                }
            }
        ).encode(),
    )
    return payload


class WebviewPreviewSourceContractTest(unittest.TestCase):
    def test_wheel_exposes_preview_entry_and_assets(self) -> None:
        project = (repository_root() / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(
            'zvec-webview-preview = "zvec_webview.app:main"',
            project,
        )
        self.assertIn('"zvec_webview*"', project)
        self.assertIn('"zvec_lan*"', project)
        self.assertIn('"frontend_dist/.vite/manifest.json"', project)
        self.assertIn('"frontend_dist/assets/*"', project)
        self.assertNotIn('zvec_webview = ["assets/*"]', project)

    def test_locked_requirements_are_exact_and_reuse_core_toolchain(self) -> None:
        requirements = (
            repository_root() / "requirements-webview-preview-lock.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("-r requirements-packaging.txt", requirements)
        for requirement in (
            "pywebview==6.2.1",
            "pythonnet==3.1.0",
            "clr_loader==0.3.1",
            "bottle==0.13.4",
            "proxy_tools==0.1.0",
            "watchdog==6.0.0",
        ):
            self.assertIn(requirement, requirements)

    def test_build_plan_is_isolated_from_official_desktop_outputs(self) -> None:
        plan = create_build_plan()
        validate_static_inputs(plan)
        self.assertEqual(plan.payload_directory.name, PRODUCT_DIRECTORY)
        self.assertIn("dist/webview-preview", plan.output_root.as_posix())
        self.assertIn("build/webview-preview", plan.work_directory.as_posix())
        self.assertNotIn("dist/desktop", plan.output_root.as_posix())
        self.assertEqual(
            plan.spec_path.name,
            "zvec_webview_preview.spec",
        )
        self.assertEqual(
            plan.requirements_path.name,
            "requirements-webview-preview-lock.txt",
        )
        self.assertEqual(plan.nsis_path.name, "Zvec.WebviewPreview.nsi")
        self.assertIsNone(plan.makensis_command)
        self.assertEqual(
            plan.installer_output.name,
            f"YaoLens-{plan.display_version}-win-x64-setup.exe",
        )
        self.assertEqual(
            plan.portable_output.name,
            f"YaoLens-{plan.display_version}-win-x64-portable.zip",
        )
        self.assertEqual(plan.to_dict()["target_runtime"], "win-x64")
        self.assertEqual(plan.frontend_directory.name, "frontend")
        self.assertEqual(plan.frontend_output.name, "frontend_dist")
        self.assertEqual(plan.frontend_manifest.name, "manifest.json")
        packaging_source = (
            plan.repository_root / "scripts" / "webview_preview_packaging.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'plan.repository_root / "image_vector_service" '
            '/ "search_learning_evaluator.py"',
            packaging_source,
        )
        for required_source in (
            "active_learning.py",
            "active_learning_review_store.py",
            "cluster_operation_store.py",
            "image_clustering.py",
            "large_cluster_adapter.py",
            "large_image_clustering.py",
            "resident_task.py",
            "search_learning_runtime.py",
            "single_instance.py",
        ):
            self.assertIn(required_source, packaging_source)
        frontend = validate_frontend_output(plan)
        self.assertEqual(frontend["manifest"], ".vite/manifest.json")

        for relative in (
            "src/App.vue",
            "src/features/activity/ActivityLogTable.vue",
            "src/features/activity/JobHistoryTable.vue",
            "src/features/activity/useActivityCenter.ts",
            "src/features/organize/OrganizePage.vue",
            "src/features/organize/galleryCapacity.ts",
            "src/features/tasks/TasksPage.vue",
        ):
            self.assertTrue((plan.frontend_directory / relative).is_file())

    def test_webview_installer_uses_an_independent_safe_identity(self) -> None:
        installer = (
            repository_root() / "installer" / "Zvec.WebviewPreview.nsi"
        ).read_text(encoding="utf-8")
        self.assertIn('!define PRODUCT_NAME "YaoLens"', installer)
        self.assertIn(
            '!define INSTALL_DIR "$LOCALAPPDATA\\Programs\\YaoLens"',
            installer,
        )
        self.assertIn('!define MAIN_EXE "YaoLens.exe"', installer)
        self.assertIn('!define PRODUCT_REG_KEY "Software\\YaoLens"', installer)
        self.assertIn(
            '"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\YaoLens"',
            installer,
        )
        self.assertIn(
            '!define LEGACY_MAIN_EXE "Zvec.WebviewPreview.exe"',
            installer,
        )
        self.assertIn(
            'DeleteRegKey /ifempty HKCU "${PRODUCT_REG_KEY}"',
            installer,
        )
        self.assertIn("RequestExecutionLevel user", installer)
        self.assertIn("SetShellVarContext current", installer)
        self.assertIn("WriteRegStr HKCU", installer)
        self.assertIn(
            '!define PRODUCT_MARKER ".zvec-webview-preview-install.ini"',
            installer,
        )
        self.assertIn('File /r "${SOURCE_DIR}\\*.*"', installer)
        self.assertIn("ReadINIStr", installer)
        self.assertIn("RMDir /r", installer)
        self.assertIn("--exit-running-instance", installer)
        self.assertIn("--install-resident-task", installer)
        self.assertIn("--remove-resident-task", installer)
        self.assertNotIn("Zvec.Desktop.exe", installer)
        self.assertNotIn("$LOCALAPPDATA\\Programs\\Zvec Desktop", installer)
        self.assertNotIn("powershell", installer.casefold())

        self.assertEqual(PRODUCT_DIRECTORY, RESIDENT_PRODUCT_DIRECTORY)
        self.assertEqual(ENTRY_POINTS[0], RESIDENT_MAIN_EXECUTABLE)
        self.assertIn(
            f'!define INSTALL_DIR "$LOCALAPPDATA\\Programs\\{PRODUCT_DIRECTORY}"',
            installer,
        )
        self.assertIn(f'!define MAIN_EXE "{RESIDENT_MAIN_EXECUTABLE}"', installer)

        install_section = installer.split(
            'Section "YaoLens" SEC_APP',
            1,
        )[1].split("SectionEnd", 1)[0]
        self.assertLess(
            install_section.index("ReadINIStr"),
            install_section.index("Call RequestZvecExit"),
        )
        self.assertLess(
            install_section.index("Call RequestZvecExit"),
            install_section.index('RMDir /r "$INSTDIR"'),
        )
        self.assertLess(
            install_section.index('File /r "${SOURCE_DIR}\\*.*"'),
            install_section.index("Call InstallResidentTask"),
        )

        uninstall_section = installer.split('Section "Uninstall"', 1)[1].split(
            "SectionEnd",
            1,
        )[0]
        self.assertLess(
            uninstall_section.index("ReadINIStr"),
            uninstall_section.index("Call un.RequestZvecExit"),
        )
        self.assertLess(
            uninstall_section.index("Call un.RequestZvecExit"),
            uninstall_section.index("Call un.EnsureZvecStopped"),
        )
        self.assertLess(
            uninstall_section.index("Call un.EnsureZvecStopped"),
            uninstall_section.index("Call un.RemoveResidentTask"),
        )
        legacy_marker_check = uninstall_section.index(
            'ReadINIStr $0 "${LEGACY_INSTALL_DIR}\\${PRODUCT_MARKER}"'
        )
        self.assertLess(
            legacy_marker_check,
            uninstall_section.index(
                'DeleteRegKey HKCU "${LEGACY_PRODUCT_UNINSTALL_KEY}"'
            ),
        )
        self.assertLess(
            uninstall_section.index("Call un.RemoveResidentTask"),
            uninstall_section.index('RMDir /r "$INSTDIR"'),
        )

    def test_installer_boundedly_waits_for_cooperative_resident_exit(self) -> None:
        installer = (
            repository_root() / "installer" / "Zvec.WebviewPreview.nsi"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Sleep 500", installer)
        self.assertNotIn("taskkill.exe", installer.casefold())
        self.assertNotIn("Stop-Process", installer)

        wait_macro = installer.split(
            "!macro WaitForProcessExit IMAGE_NAME",
            1,
        )[1].split("!macroend", 1)[0]
        uninstall_wait_macro = installer.split(
            "!macro un.WaitForProcessExit IMAGE_NAME",
            1,
        )[1].split("!macroend", 1)[0]
        for wait in (wait_macro, uninstall_wait_macro):
            with self.subTest(wait=wait.splitlines()[0:2]):
                self.assertIn("$SYSDIR\\tasklist.exe", wait)
                self.assertIn("StrCpy $3 0", wait)
                self.assertIn("IntOp $3 $3 + 1", wait)
                self.assertIn('${If} $0 != "0"\n      ${ExitDo}', wait)
                self.assertIn('${If} $2 == ""\n      ${ExitDo}', wait)
                attempts = int(wait.split("${If} $3 >= ", 1)[1].splitlines()[0].strip())
                interval_ms = int(wait.split("Sleep ", 1)[1].splitlines()[0].strip())
                self.assertEqual(attempts, 40)
                self.assertEqual(interval_ms, 250)
                self.assertLessEqual(attempts * interval_ms, 10_000)

        request_exit = installer.split("Function RequestZvecExit", 1)[1].split(
            "FunctionEnd",
            1,
        )[0]
        self.assertLess(
            request_exit.index("--exit-running-instance"),
            request_exit.index('!insertmacro WaitForProcessExit "${MAIN_EXE}"'),
        )
        uninstall_request_exit = installer.split(
            "Function un.RequestZvecExit",
            1,
        )[1].split("FunctionEnd", 1)[0]
        self.assertLess(
            uninstall_request_exit.index("--exit-running-instance"),
            uninstall_request_exit.index(
                '!insertmacro un.WaitForProcessExit "${MAIN_EXE}"'
            ),
        )

        install_section = installer.split(
            'Section "YaoLens" SEC_APP',
            1,
        )[1].split("SectionEnd", 1)[0]
        request_index = install_section.index("Call RequestZvecExit")
        self.assertLess(
            request_index,
            install_section.index("Call EnsureZvecStopped", request_index),
        )
        uninstall_section = installer.split('Section "Uninstall"', 1)[1].split(
            "SectionEnd",
            1,
        )[0]
        self.assertLess(
            uninstall_section.index("Call un.RequestZvecExit"),
            uninstall_section.index("Call un.EnsureZvecStopped"),
        )

    def test_first_run_state_uses_current_user_writable_directories(self) -> None:
        root = repository_root()
        launcher = (root / "zvec_launcher.py").read_text(encoding="utf-8")
        activity = (root / "image_vector_service" / "activity_store.py").read_text(
            encoding="utf-8"
        )
        app = (root / "zvec_webview" / "app.py").read_text(encoding="utf-8")

        self.assertIn('os.environ["LOCALAPPDATA"], "zvec-image-search"', launcher)
        self.assertIn("self._config_home.mkdir(parents=True, exist_ok=True)", activity)
        self.assertIn('connection.execute("PRAGMA journal_mode=WAL")', activity)
        self.assertIn(
            'storage_path=str(runtime.facade.config_home / "webview")',
            app,
        )

    def test_makensis_plan_targets_only_the_dedicated_webview_installer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            compiler = Path(temporary) / "makensis.exe"
            compiler.write_bytes(b"synthetic compiler")
            plan = create_build_plan(makensis=compiler)
            validate_static_inputs(plan)

        self.assertEqual(
            plan.makensis_command,
            (
                str(compiler.resolve()),
                f"/DVERSION={plan.version}",
                f"/DDISPLAY_VERSION={plan.display_version}",
                f"/DFILE_VERSION={windows_file_version(plan.version)}",
                f"/DSOURCE_DIR={plan.payload_directory}",
                f"/DOUTPUT_FILE={plan.installer_output}",
                "/DRID=win-x64",
                str(plan.nsis_path),
            ),
        )
        self.assertIn("dist/webview-preview", plan.installer_output.as_posix())
        self.assertNotIn("dist/desktop", plan.installer_output.as_posix())

    def test_windows_file_version_is_four_numeric_parts(self) -> None:
        self.assertEqual(windows_file_version("0.4.0"), "0.4.0.0")
        self.assertEqual(windows_file_version("1.2.3-preview.4"), "1.2.3.4")
        with self.assertRaisesRegex(
            WebviewPreviewPackagingError,
            "at least three numeric parts",
        ):
            windows_file_version("1.2")

    def test_public_version_compacts_zero_patch_stable_semver(self) -> None:
        self.assertEqual(public_version("0.1.0"), "0.1")
        self.assertEqual(public_version("1.2.3"), "1.2.3")
        self.assertEqual(public_version("1.2.0-rc.1"), "1.2.0-rc.1")
        with self.assertRaisesRegex(
            WebviewPreviewPackagingError,
            "three-part SemVer",
        ):
            public_version("0.1")

    def test_spec_has_one_graph_three_executables_and_required_assets(self) -> None:
        spec = (
            repository_root()
            / "release"
            / "webview_preview"
            / "zvec_webview_preview.spec"
        ).read_text(encoding="utf-8")
        self.assertEqual(spec.count("Analysis("), 1)
        self.assertEqual(spec.count("COLLECT("), 1)
        self.assertEqual(spec.count("version=str(VERSION_INFO)"), 3)
        for executable in ("YaoLens", "zvec", "zvec-backend"):
            self.assertIn(f'name="{executable}"', spec)
        self.assertIn("zvec_webview/frontend_dist", spec)
        self.assertNotIn("zvec_webview/assets", spec)
        self.assertIn("model-catalog.default.json", spec)
        self.assertIn("Zvec.AppIcon.ico", spec)
        self.assertIn('"image_vector_service.activity_store"', spec)
        self.assertIn('"image_vector_service.active_learning"', spec)
        self.assertIn('"image_vector_service.active_learning_review_store"', spec)
        self.assertIn('"image_vector_service.cluster_operation_store"', spec)
        self.assertIn('"image_vector_service.data_migration"', spec)
        self.assertIn('"image_vector_service.migration_recovery"', spec)
        self.assertIn('"image_vector_service.folder_deletion"', spec)
        self.assertIn('"image_vector_service.image_clustering"', spec)
        self.assertIn('"image_vector_service.large_cluster_adapter"', spec)
        self.assertIn('"image_vector_service.large_image_clustering"', spec)
        self.assertIn('"image_vector_service.library_browser"', spec)
        self.assertIn('"image_vector_service.search_learning_service"', spec)

        version_info = (
            repository_root()
            / "release"
            / "webview_preview"
            / "yaolens_version_info.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("filevers=(0, 2, 0, 0)", version_info)
        self.assertIn('StringStruct("ProductName", "YaoLens")', version_info)
        self.assertIn('StringStruct("FileVersion", "0.2.0")', version_info)
        self.assertIn('StringStruct("ProductVersion", "0.2.0")', version_info)
        self.assertIn('"image_vector_service.search_learning_evaluator"', spec)
        self.assertIn('"image_vector_service.search_learning_store"', spec)
        self.assertIn('"image_vector_service.search_learning_runtime"', spec)
        self.assertIn('"zvec_lan.http_server"', spec)
        self.assertIn('"zvec_lan.service"', spec)
        self.assertIn('"zvec_webview.lan_access"', spec)
        self.assertIn('"zvec_webview.resident_task"', spec)
        self.assertIn('"zvec_webview.single_instance"', spec)
        self.assertIn('name="YaoLens"', spec)
        self.assertIn('"tkinter"', spec)
        self.assertIn('"pystray"', spec)
        for unsupported_platform in ("android", "cocoa", "gtk", "qt"):
            self.assertIn(f'"webview.platforms.{unsupported_platform}"', spec)
        frozen_entry = (
            repository_root() / "release" / "webview_preview" / "frozen_entry.py"
        ).read_text(encoding="utf-8")
        for runtime_module in (
            "image_vector_service.active_learning",
            "image_vector_service.active_learning_review_store",
            "image_vector_service.cluster_operation_store",
            "image_vector_service.file_watcher",
            "image_vector_service.image_clustering",
            "image_vector_service.large_cluster_adapter",
            "image_vector_service.large_image_clustering",
            "image_vector_service.search_learning_runtime",
        ):
            self.assertIn(f"import {runtime_module}", frozen_entry)
        self.assertIn("ClusterOperationStore(", frozen_entry)
        self.assertIn("ActiveLearningReviewStore(", frozen_entry)

    def test_frozen_self_test_requires_activity_modules_and_sqlite_wal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = create_build_plan(
                output_root=root / "output",
                work_directory=root / "work",
            )
            modules = [
                "PIL.Image",
                "clr",
                "image_vector_service.active_learning",
                "image_vector_service.active_learning_review_store",
                "image_vector_service.activity_store",
                "image_vector_service.cluster_operation_store",
                "image_vector_service.data_migration",
                "image_vector_service.migration_recovery",
                "image_vector_service.folder_deletion",
                "image_vector_service.file_watcher",
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
                "zvec_lan.discovery",
                "zvec_lan.http_server",
                "zvec_lan.models",
                "zvec_lan.pairing",
                "zvec_lan.service",
                "zvec_lan.uploads",
                "webview",
                "webview.platforms.edgechromium",
                "webview.platforms.winforms",
                "zvec",
                "zvec_webview.app",
                "zvec_webview.facade",
                "zvec_webview.frontend_assets",
                "zvec_webview.lan_access",
                "zvec_webview.lan_settings",
                "zvec_webview.native_bridge",
                "zvec_webview.runtime",
                "zvec_webview.server",
                "watchdog.observers",
            ]

            def completed(command: list[str], **_kwargs: object) -> object:
                output = Path(command[2])
                output.write_text(
                    json.dumps(
                        {
                            "status": "ok",
                            "asset_count": 9,
                            "modules": modules,
                            "sqlite": {
                                "module": "sqlite3",
                                "journal_mode": "wal",
                                "row_count": 1,
                            },
                            "persistence": {
                                "cluster_store_schema": 1,
                                "cluster_store_api_requests": 0,
                                "active_learning_recovered": 0,
                            },
                            "frontend": {
                                "manifest": ".vite/manifest.json",
                                "entry_key": "index.html",
                                "files": [
                                    "index.html",
                                    ".vite/manifest.json",
                                    "assets/index.js",
                                    "assets/index.css",
                                ],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                return types.SimpleNamespace(returncode=0)

            with patch.object(
                build_webview_preview.subprocess,
                "run",
                side_effect=completed,
            ):
                result = build_webview_preview._run_frozen_self_test(plan)

        self.assertEqual(result["sqlite"]["journal_mode"], "wal")
        self.assertIn("image_vector_service.activity_store", result["modules"])
        self.assertIn("image_vector_service.active_learning", result["modules"])
        self.assertIn(
            "image_vector_service.active_learning_review_store",
            result["modules"],
        )
        self.assertIn(
            "image_vector_service.cluster_operation_store",
            result["modules"],
        )
        self.assertIn("image_vector_service.data_migration", result["modules"])
        self.assertIn("image_vector_service.migration_recovery", result["modules"])
        self.assertIn("image_vector_service.library_browser", result["modules"])
        self.assertIn("image_vector_service.file_watcher", result["modules"])
        self.assertIn("image_vector_service.image_clustering", result["modules"])
        self.assertIn(
            "image_vector_service.large_cluster_adapter",
            result["modules"],
        )
        self.assertIn(
            "image_vector_service.large_image_clustering",
            result["modules"],
        )
        self.assertIn("image_vector_service.search_learning_store", result["modules"])
        self.assertIn(
            "image_vector_service.search_learning_evaluator",
            result["modules"],
        )
        self.assertIn("watchdog.observers", result["modules"])
        self.assertEqual(result["persistence"]["cluster_store_schema"], 1)
        self.assertEqual(result["persistence"]["cluster_store_api_requests"], 0)
        self.assertEqual(result["persistence"]["active_learning_recovered"], 0)

    def test_frontend_pipeline_is_clean_typechecked_tested_and_built(self) -> None:
        plan = create_build_plan()
        attributes = (plan.repository_root / ".gitattributes").read_text(
            encoding="utf-8"
        )
        vite_config = (plan.frontend_directory / "vite.config.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn("frontend/** text eol=lf", attributes)
        self.assertIn("zvec_webview/frontend_dist/** text eol=lf", attributes)
        self.assertIn('componentIdGenerator: "filepath"', vite_config)
        expected = {"manifest": ".vite/manifest.json"}
        with (
            patch.object(
                build_webview_preview.shutil,
                "which",
                side_effect=lambda name: (
                    "C:/node/npm.cmd" if name == "npm.cmd" else None
                ),
            ),
            patch.object(build_webview_preview, "_run") as run,
            patch.object(
                build_webview_preview,
                "validate_frontend_output",
                return_value=expected,
            ),
        ):
            self.assertEqual(build_webview_preview._build_frontend(plan), expected)
        self.assertEqual(
            run.call_args_list,
            [
                call(("C:/node/npm.cmd", "ci"), cwd=plan.frontend_directory),
                call(
                    ("C:/node/npm.cmd", "run", "typecheck"),
                    cwd=plan.frontend_directory,
                ),
                call(("C:/node/npm.cmd", "test"), cwd=plan.frontend_directory),
                call(
                    ("C:/node/npm.cmd", "run", "build"),
                    cwd=plan.frontend_directory,
                ),
            ],
        )

    def test_approved_frontend_can_be_frozen_without_rebuilding_it(self) -> None:
        plan = create_build_plan()
        expected = {"manifest": ".vite/manifest.json"}
        with (
            patch.object(build_webview_preview, "_build_frontend") as rebuild,
            patch.object(
                build_webview_preview,
                "validate_frontend_output",
                return_value=expected,
            ) as validate,
        ):
            result = build_webview_preview._prepare_frontend(
                plan,
                rebuild=False,
            )

        self.assertEqual(result, expected)
        rebuild.assert_not_called()
        validate.assert_called_once_with(plan)

    def test_custom_webview_hook_contains_only_x64_native_loader(self) -> None:
        hook = (
            repository_root()
            / "release"
            / "webview_preview"
            / "hooks"
            / "hook-webview.py"
        ).read_text(encoding="utf-8")
        self.assertIn("win-x64/native/WebView2Loader.dll", hook)
        self.assertNotIn("win-x86/native/WebView2Loader.dll", hook)
        self.assertNotIn("win-arm64/native/WebView2Loader.dll", hook)
        self.assertIn("WebBrowserInterop.x64.dll", hook)
        self.assertNotIn("WebBrowserInterop.x86.dll", hook)

    def test_runtime_hook_maps_all_pywebview_probes_to_x64(self) -> None:
        for value in ("win-arm64", "win-x64", "win-x86"):
            self.assertEqual(
                runtime_hook._x64_pywebview_runtime_name(value),
                "win-x64",
            )
        self.assertEqual(
            runtime_hook._x64_pywebview_runtime_name("Microsoft.Web.WebView2.Core.dll"),
            "Microsoft.Web.WebView2.Core.dll",
        )


class WebviewPreviewHostContractTest(unittest.TestCase):
    def test_supported_host_has_no_errors(self) -> None:
        self.assertEqual(
            target_host_errors(
                os_name="nt",
                platform_name="win32",
                implementation="CPython",
                python_version=(3, 12),
                machine="AMD64",
                pointer_size=8,
            ),
            (),
        )

    def test_mac_linux_arm_pypy_and_wrong_python_are_rejected(self) -> None:
        cases = (
            ("posix", "linux", "CPython", (3, 12), "x86_64", 8, "Windows only"),
            ("posix", "darwin", "CPython", (3, 12), "x86_64", 8, "Windows only"),
            ("nt", "win32", "CPython", (3, 12), "ARM64", 8, "Windows x64"),
            ("nt", "win32", "PyPy", (3, 12), "AMD64", 8, "CPython"),
            ("nt", "win32", "CPython", (3, 11), "AMD64", 8, "3.12"),
        )
        for (
            os_name,
            platform_name,
            implementation,
            version,
            machine,
            pointer_size,
            expected,
        ) in cases:
            with self.subTest(platform=platform_name, machine=machine):
                errors = target_host_errors(
                    os_name=os_name,
                    platform_name=platform_name,
                    implementation=implementation,
                    python_version=version,
                    machine=machine,
                    pointer_size=pointer_size,
                )
                self.assertIn(expected, "\n".join(errors))


class WebviewPreviewPayloadContractTest(unittest.TestCase):
    def test_valid_payload_manifest_and_portable_archive_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = _valid_payload(root)
            self.assertTrue(inspect_payload(payload).is_valid)

            manifest_path = write_payload_manifest(payload, version="0.4.0")
            manifest = verify_payload_manifest(payload)
            self.assertEqual(
                manifest_path.name,
                "yaolens-payload-manifest.json",
            )
            self.assertEqual(manifest["entry_points"], list(ENTRY_POINTS))
            self.assertEqual(manifest["target_runtime"], "win-x64")

            archive_path = root / "preview.zip"
            result = write_portable_archive(payload, archive_path)
            self.assertGreater(result["size"], 0)
            self.assertEqual(len(result["sha256"]), 64)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertIn("YaoLens.exe", archive.namelist())
                self.assertIsNone(archive.testzip())

    def test_manifest_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            payload = _valid_payload(Path(temporary))
            write_payload_manifest(payload, version="0.4.0")
            (payload / "zvec.exe").write_bytes(b"changed")
            with self.assertRaisesRegex(
                WebviewPreviewPackagingError,
                "size mismatch|SHA-256 mismatch",
            ):
                verify_payload_manifest(payload)

    def test_payload_rejects_non_x64_webview_and_legacy_tk_files(self) -> None:
        cases = (
            (
                "_internal/webview/lib/runtimes/win-arm64/native/WebView2Loader.dll",
                "Non-x64 WebView2",
            ),
            (
                "_internal/webview/lib/runtimes/win-x86/native/WebView2Loader.dll",
                "Non-x64 WebView2",
            ),
            ("_internal/webview/lib/WebBrowserInterop.x86.dll", "Forbidden"),
            ("_internal/_tkinter.pyd", "Legacy Tk"),
            ("_internal/tcl86t.dll", "Legacy Tk"),
            ("Zvec.Desktop.exe", "Forbidden"),
            ("Zvec.WebviewPreview.exe", "Forbidden"),
            (
                "_internal/zvec_webview/frontend_dist/node_modules/vue/index.js",
                "node_modules",
            ),
            (
                "_internal/zvec_webview/assets/app.js",
                "Legacy web assets",
            ),
        )
        for relative, expected in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temp:
                payload = _valid_payload(Path(temp))
                _write(payload, relative)
                errors = "\n".join(inspect_payload(payload).errors)
                self.assertIn(expected, errors)


class WebviewPreviewFrozenEntryTest(unittest.TestCase):
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

    def test_three_executable_names_dispatch_to_expected_entry_points(self) -> None:
        cases = (
            ("YaoLens.exe", "zvec_webview.app", ["--debug"], 10),
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

    def test_backend_defaults_to_serve_and_unknown_exe_fails(self) -> None:
        calls: list[list[str]] = []
        module = self._module("image_service", 0, calls)
        with patch.dict(sys.modules, {"image_service": module}):
            self.assertEqual(
                frozen_entry.main([], executable="zvec-backend.exe"),
                0,
            )
        self.assertEqual(calls, [["serve"]])
        with self.assertRaises(frozen_entry.FrozenWebviewEntryError):
            frozen_entry.main([], executable="unexpected.exe")

    def test_packaging_self_test_is_intercepted_before_app_dispatch(self) -> None:
        output = Path("self-test.json")
        with patch.object(
            frozen_entry,
            "_run_packaging_self_test",
            return_value=17,
        ) as self_test:
            result = frozen_entry.main(
                ["--zvec-packaging-self-test", str(output)],
                executable="YaoLens.exe",
            )
        self.assertEqual(result, 17)
        self_test.assert_called_once_with(output)

    def test_legacy_python_module_backend_contract_is_supported(self) -> None:
        calls: list[list[str]] = []
        module = self._module("image_service", 7, calls)
        with patch.dict(sys.modules, {"image_service": module}):
            result = frozen_entry.main(
                ["-m", "image_service", "serve", "--port", "1234"],
                executable="YaoLens.exe",
            )
        self.assertEqual(result, 7)
        self.assertEqual(calls, [["serve", "--port", "1234"]])


if __name__ == "__main__":
    unittest.main()
