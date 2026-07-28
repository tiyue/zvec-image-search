from __future__ import annotations

import os
import subprocess
import sys
import unittest

from zvec_host.windows_job import (
    WindowsJobError,
    WindowsKillOnCloseJob,
    create_kill_on_close_job,
)


class _FakeWindowsJobApi:
    def __init__(self, *, fail_configuration: bool = False) -> None:
        self.fail_configuration = fail_configuration
        self.created = 0
        self.configured: list[int] = []
        self.assignments: list[tuple[int, int]] = []
        self.closed: list[int] = []

    def create(self) -> int:
        self.created += 1
        return 123

    def enable_kill_on_close(self, handle: int) -> None:
        self.configured.append(handle)
        if self.fail_configuration:
            raise WindowsJobError(
                "SetInformationJobObject",
                winerror=5,
                detail="test configuration failure",
            )

    def assign(self, job_handle: int, process_handle: int) -> None:
        self.assignments.append((job_handle, process_handle))

    def close(self, handle: int) -> None:
        self.closed.append(handle)


class _FakeProcess:
    _handle = 456


class WindowsJobTests(unittest.TestCase):
    def test_injected_boundary_configures_assigns_and_closes(self) -> None:
        api = _FakeWindowsJobApi()
        job = WindowsKillOnCloseJob.create(api=api)

        job.assign(_FakeProcess())  # type: ignore[arg-type]
        job.close()
        job.close()

        self.assertEqual(api.created, 1)
        self.assertEqual(api.configured, [123])
        self.assertEqual(api.assignments, [(123, 456)])
        self.assertEqual(api.closed, [123])
        self.assertTrue(job.closed)

    def test_configuration_failure_closes_created_handle(self) -> None:
        api = _FakeWindowsJobApi(fail_configuration=True)

        with self.assertRaises(WindowsJobError):
            WindowsKillOnCloseJob.create(api=api)

        self.assertEqual(api.created, 1)
        self.assertEqual(api.configured, [123])
        self.assertEqual(api.closed, [123])

    @unittest.skipUnless(os.name == "nt", "requires Windows Job Objects")
    def test_closing_real_job_terminates_assigned_child(self) -> None:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        try:
            job = create_kill_on_close_job()
            job.assign(process)
            job.close()
            process.wait(timeout=5.0)
            self.assertIsNotNone(process.returncode)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
