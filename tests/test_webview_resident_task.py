from __future__ import annotations

import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from zvec_webview.resident_task import (
    LEGACY_TASK_NAME,
    MAIN_EXECUTABLE,
    PRODUCT_ID,
    PRODUCT_MARKER,
    RESTART_COUNT,
    RESTART_INTERVAL,
    START_HIDDEN_ARGUMENT,
    TASK_NAME,
    ResidentTaskError,
    build_task_xml,
    install_resident_task,
    remove_resident_task,
    resolve_current_user_sid,
)

_TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_SID = "S-1-5-21-111-222-333-1001"
_TASK_NOT_FOUND = "ERROR: The system cannot find the file specified."
_TASK_NOT_FOUND_ZH = "错误: 系统找不到指定的文件。"


def _tag(name: str) -> str:
    return f"{{{_TASK_NAMESPACE}}}{name}"


def _prepare_installation(root: Path, *, product_id: str = PRODUCT_ID) -> Path:
    install = root / "Local & App" / "Programs" / "YaoLens"
    install.mkdir(parents=True)
    (install / MAIN_EXECUTABLE).write_bytes(b"installed preview")
    (install / PRODUCT_MARKER).write_text(
        f"[ZvecWebviewPreview]\nProductId={product_id}\nVersion=0.5.0\n",
        encoding="utf-16",
    )
    return install


def _prepare_system_root(root: Path) -> Path:
    system_root = root / "Windows"
    system32 = system_root / "System32"
    system32.mkdir(parents=True)
    (system32 / "schtasks.exe").write_bytes(b"system schtasks")
    (system32 / "whoami.exe").write_bytes(b"system whoami")
    return system_root


class RecordingRunner:
    def __init__(
        self,
        responses: list[subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.task_xml: bytes | None = None
        self.task_xml_path: Path | None = None

    def __call__(
        self,
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), dict(kwargs)))
        if "/XML" in command:
            path = Path(command[command.index("/XML") + 1])
            self.task_xml_path = path
            self.task_xml = path.read_bytes()
        if self.responses:
            return self.responses.pop(0)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class WebviewResidentTaskXmlTest(unittest.TestCase):
    def test_xml_uses_current_user_logon_and_bounded_restart_contract(self) -> None:
        executable = Path(r"C:\Users\A&B\YaoLens") / MAIN_EXECUTABLE
        working_directory = executable.parent

        encoded = build_task_xml(executable, working_directory, _SID)
        document = ET.fromstring(encoded)

        self.assertEqual(document.attrib["version"], "1.4")
        logon = document.find(f"{_tag('Triggers')}/{_tag('LogonTrigger')}")
        assert logon is not None
        self.assertEqual(logon.findtext(_tag("Enabled")), "true")
        self.assertEqual(logon.findtext(_tag("UserId")), _SID)

        principal = document.find(f"{_tag('Principals')}/{_tag('Principal')}")
        assert principal is not None
        self.assertEqual(principal.attrib["id"], "CurrentUser")
        self.assertEqual(principal.findtext(_tag("UserId")), _SID)
        self.assertEqual(
            principal.findtext(_tag("LogonType")),
            "InteractiveToken",
        )
        self.assertEqual(principal.findtext(_tag("RunLevel")), "LeastPrivilege")

        settings = document.find(_tag("Settings"))
        assert settings is not None
        self.assertEqual(
            settings.findtext(_tag("MultipleInstancesPolicy")),
            "IgnoreNew",
        )
        self.assertEqual(settings.findtext(_tag("ExecutionTimeLimit")), "PT0S")
        restart = settings.find(_tag("RestartOnFailure"))
        assert restart is not None
        self.assertEqual(restart.findtext(_tag("Interval")), RESTART_INTERVAL)
        self.assertEqual(restart.findtext(_tag("Count")), str(RESTART_COUNT))

        action = document.find(f"{_tag('Actions')}/{_tag('Exec')}")
        assert action is not None
        self.assertEqual(action.findtext(_tag("Command")), str(executable.absolute()))
        self.assertEqual(action.findtext(_tag("Arguments")), START_HIDDEN_ARGUMENT)
        self.assertEqual(
            action.findtext(_tag("WorkingDirectory")),
            str(working_directory.absolute()),
        )
        self.assertEqual(
            document.findtext(f"{_tag('RegistrationInfo')}/{_tag('Description')}"),
            "Starts YaoLens after the current user logs on.",
        )
        self.assertIn("&amp;", encoded.decode("utf-16"))

    def test_invalid_sid_is_rejected_before_xml_is_built(self) -> None:
        with self.assertRaisesRegex(ResidentTaskError, "valid current-user SID"):
            build_task_xml(Path("YaoLens.exe"), Path("."), "not-a-sid")


class WebviewResidentTaskInstallTest(unittest.TestCase):
    def test_install_validates_marker_and_uses_system_schtasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = _prepare_installation(root)
            system_root = _prepare_system_root(root)
            runner = RecordingRunner(
                [
                    subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                    subprocess.CompletedProcess(
                        [], 1, stdout="", stderr=_TASK_NOT_FOUND
                    ),
                ]
            )

            install_resident_task(
                install,
                user_sid=_SID,
                runner=runner,
                system_root=system_root,
                environment={"LOCALAPPDATA": str(install.parents[1])},
                temp_directory=root,
            )

            self.assertEqual(len(runner.calls), 2)
            command, options = runner.calls[0]
            self.assertEqual(
                command,
                [
                    str(system_root / "System32" / "schtasks.exe"),
                    "/Create",
                    "/TN",
                    TASK_NAME,
                    "/XML",
                    str(runner.task_xml_path),
                    "/F",
                ],
            )
            self.assertEqual(
                options["creationflags"],
                getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertFalse(runner.task_xml_path.exists())
            assert runner.task_xml is not None
            action = ET.fromstring(runner.task_xml).find(
                f"{_tag('Actions')}/{_tag('Exec')}"
            )
            assert action is not None
            self.assertEqual(
                action.findtext(_tag("Command")),
                str(install / MAIN_EXECUTABLE),
            )
            self.assertEqual(
                action.findtext(_tag("Arguments")),
                START_HIDDEN_ARGUMENT,
            )
            self.assertEqual(
                runner.calls[1][0],
                [
                    str(system_root / "System32" / "schtasks.exe"),
                    "/Query",
                    "/TN",
                    LEGACY_TASK_NAME,
                ],
            )

    def test_install_removes_the_legacy_task_after_creating_the_new_task(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = _prepare_installation(root)
            system_root = _prepare_system_root(root)
            runner = RecordingRunner(
                [
                    subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="task", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                ]
            )

            install_resident_task(
                install,
                user_sid=_SID,
                runner=runner,
                system_root=system_root,
                environment={"LOCALAPPDATA": str(install.parents[1])},
                temp_directory=root,
            )

            tool = str(system_root / "System32" / "schtasks.exe")
            self.assertEqual(
                [call[0] for call in runner.calls[1:]],
                [
                    [tool, "/Query", "/TN", LEGACY_TASK_NAME],
                    [tool, "/Delete", "/TN", LEGACY_TASK_NAME, "/F"],
                ],
            )

    def test_install_rolls_back_new_task_when_legacy_delete_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = _prepare_installation(root)
            system_root = _prepare_system_root(root)
            runner = RecordingRunner(
                [
                    subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="legacy", stderr=""),
                    subprocess.CompletedProcess(
                        [],
                        1,
                        stdout="",
                        stderr="ERROR: legacy task delete was denied.",
                    ),
                    subprocess.CompletedProcess([], 0, stdout="current", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                ]
            )

            with self.assertRaisesRegex(
                ResidentTaskError,
                "Could not remove the legacy YaoLens resident task",
            ) as raised:
                install_resident_task(
                    install,
                    user_sid=_SID,
                    runner=runner,
                    system_root=system_root,
                    environment={"LOCALAPPDATA": str(install.parents[1])},
                    temp_directory=root,
                )

            self.assertIsNone(raised.exception.__cause__)
            self.assertFalse(runner.task_xml_path.exists())
            tool = str(system_root / "System32" / "schtasks.exe")
            self.assertEqual(
                [call[0] for call in runner.calls[1:]],
                [
                    [tool, "/Query", "/TN", LEGACY_TASK_NAME],
                    [tool, "/Delete", "/TN", LEGACY_TASK_NAME, "/F"],
                    [tool, "/Query", "/TN", TASK_NAME],
                    [tool, "/Delete", "/TN", TASK_NAME, "/F"],
                ],
            )

    def test_install_keeps_legacy_failure_when_new_task_rollback_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = _prepare_installation(root)
            system_root = _prepare_system_root(root)
            runner = RecordingRunner(
                [
                    subprocess.CompletedProcess([], 0, stdout="", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="legacy", stderr=""),
                    subprocess.CompletedProcess(
                        [],
                        1,
                        stdout="",
                        stderr="ERROR: legacy task delete was denied.",
                    ),
                    subprocess.CompletedProcess([], 0, stdout="current", stderr=""),
                    subprocess.CompletedProcess(
                        [],
                        1,
                        stdout="",
                        stderr="ERROR: current task rollback was denied.",
                    ),
                ]
            )

            with self.assertRaisesRegex(
                ResidentTaskError,
                "Could not remove the legacy YaoLens resident task",
            ) as raised:
                install_resident_task(
                    install,
                    user_sid=_SID,
                    runner=runner,
                    system_root=system_root,
                    environment={"LOCALAPPDATA": str(install.parents[1])},
                    temp_directory=root,
                )

            rollback_error = raised.exception.__cause__
            self.assertIsInstance(rollback_error, ResidentTaskError)
            assert rollback_error is not None
            self.assertIn(
                "Could not roll back the newly-created YaoLens resident task",
                str(rollback_error),
            )
            self.assertIn("current task rollback was denied", str(rollback_error))
            self.assertFalse(runner.task_xml_path.exists())
            tool = str(system_root / "System32" / "schtasks.exe")
            self.assertEqual(
                [call[0] for call in runner.calls[1:]],
                [
                    [tool, "/Query", "/TN", LEGACY_TASK_NAME],
                    [tool, "/Delete", "/TN", LEGACY_TASK_NAME, "/F"],
                    [tool, "/Query", "/TN", TASK_NAME],
                    [tool, "/Delete", "/TN", TASK_NAME, "/F"],
                ],
            )

    def test_install_rejects_portable_path_and_wrong_product_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = _prepare_installation(root / "expected")
            portable = _prepare_installation(root / "portable")
            system_root = _prepare_system_root(root)
            runner = RecordingRunner()

            with self.assertRaisesRegex(ResidentTaskError, "restricted to"):
                install_resident_task(
                    portable,
                    user_sid=_SID,
                    runner=runner,
                    system_root=system_root,
                    environment={"LOCALAPPDATA": str(expected.parents[1])},
                )
            self.assertEqual(runner.calls, [])

            (expected / PRODUCT_MARKER).write_text(
                "[ZvecWebviewPreview]\nProductId={WRONG}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ResidentTaskError, "wrong ProductId"):
                install_resident_task(
                    expected,
                    user_sid=_SID,
                    runner=runner,
                    system_root=system_root,
                    environment={"LOCALAPPDATA": str(expected.parents[1])},
                )
            self.assertEqual(runner.calls, [])

    def test_failed_create_still_removes_temporary_xml(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = _prepare_installation(root)
            system_root = _prepare_system_root(root)
            runner = RecordingRunner(
                [
                    subprocess.CompletedProcess(
                        [],
                        1,
                        stdout="",
                        stderr="create failed",
                    )
                ]
            )

            with self.assertRaisesRegex(ResidentTaskError, "create failed"):
                install_resident_task(
                    install,
                    user_sid=_SID,
                    runner=runner,
                    system_root=system_root,
                    environment={"LOCALAPPDATA": str(install.parents[1])},
                    temp_directory=root,
                )

            assert runner.task_xml_path is not None
            self.assertFalse(runner.task_xml_path.exists())

    def test_sid_resolution_is_injectable_and_uses_system_whoami(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_root = _prepare_system_root(root)
            runner = RecordingRunner(
                [
                    subprocess.CompletedProcess(
                        [],
                        0,
                        stdout=f'"DESKTOP\\\\user","{_SID}"\n',
                        stderr="",
                    )
                ]
            )

            actual = resolve_current_user_sid(
                runner=runner,
                system_root=system_root,
            )

            self.assertEqual(actual, _SID)
            self.assertEqual(
                runner.calls[0][0],
                [
                    str(system_root / "System32" / "whoami.exe"),
                    "/user",
                    "/fo",
                    "csv",
                    "/nh",
                ],
            )


class WebviewResidentTaskRemoveTest(unittest.TestCase):
    def test_remove_queries_then_deletes_the_owned_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_root = _prepare_system_root(root)
            runner = RecordingRunner(
                [
                    subprocess.CompletedProcess([], 0, stdout="task", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="deleted", stderr=""),
                    subprocess.CompletedProcess(
                        [], 1, stdout="", stderr=_TASK_NOT_FOUND
                    ),
                ]
            )

            removed = remove_resident_task(
                runner=runner,
                system_root=system_root,
            )

            self.assertTrue(removed)
            tool = str(system_root / "System32" / "schtasks.exe")
            self.assertEqual(
                [call[0] for call in runner.calls],
                [
                    [tool, "/Query", "/TN", TASK_NAME],
                    [tool, "/Delete", "/TN", TASK_NAME, "/F"],
                    [tool, "/Query", "/TN", LEGACY_TASK_NAME],
                ],
            )

    def test_remove_deletes_both_current_and_legacy_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_root = _prepare_system_root(root)
            runner = RecordingRunner(
                [
                    subprocess.CompletedProcess([], 0, stdout="task", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="deleted", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="task", stderr=""),
                    subprocess.CompletedProcess([], 0, stdout="deleted", stderr=""),
                ]
            )

            self.assertTrue(
                remove_resident_task(runner=runner, system_root=system_root)
            )

            tool = str(system_root / "System32" / "schtasks.exe")
            self.assertEqual(
                [call[0] for call in runner.calls],
                [
                    [tool, "/Query", "/TN", TASK_NAME],
                    [tool, "/Delete", "/TN", TASK_NAME, "/F"],
                    [tool, "/Query", "/TN", LEGACY_TASK_NAME],
                    [tool, "/Delete", "/TN", LEGACY_TASK_NAME, "/F"],
                ],
            )

    def test_remove_is_idempotent_when_the_task_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            system_root = _prepare_system_root(root)
            runner = RecordingRunner(
                [
                    subprocess.CompletedProcess(
                        [],
                        1,
                        stdout="",
                        stderr=_TASK_NOT_FOUND_ZH,
                    ),
                    subprocess.CompletedProcess(
                        [],
                        1,
                        stdout="",
                        stderr=_TASK_NOT_FOUND,
                    ),
                ]
            )

            self.assertFalse(
                remove_resident_task(
                    runner=runner,
                    system_root=system_root,
                )
            )
            self.assertEqual(len(runner.calls), 2)

    def test_remove_rejects_non_missing_query_failures(self) -> None:
        failures = (
            "ERROR: Access is denied.",
            "ERROR: The Task Scheduler service is not available.",
        )
        for detail in failures:
            with (
                self.subTest(detail=detail),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                system_root = _prepare_system_root(root)
                runner = RecordingRunner(
                    [
                        subprocess.CompletedProcess(
                            [],
                            1,
                            stdout="",
                            stderr=detail,
                        )
                    ]
                )

                with self.assertRaisesRegex(
                    ResidentTaskError,
                    "Could not query before remove the YaoLens resident task",
                ):
                    remove_resident_task(
                        runner=runner,
                        system_root=system_root,
                    )
                self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
