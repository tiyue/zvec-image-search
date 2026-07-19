from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from image_vector_service.activity_store import ActivityStore, JobHistoryRecord
from image_vector_service.backend_server import BackendHTTPServer, BackendJobManager
from image_vector_service.config import RuntimeCredentials
from zvec_desktop.configuration_service import DesktopConfigurationService
from zvec_desktop.credentials import SessionCredentialStore
from zvec_desktop.library_tasks import SubmittedLibraryTask
from zvec_webview.facade import PreviewFacade, _TaskOperation
from zvec_webview.server import GatewayServer


class _IdleHost:
    is_running = False

    def stop(self, *, force: bool = False) -> None:
        del force


class _OrderedClose:
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._calls.append(self._name)
            self._closed = True

    def release(self) -> None:
        self.close()


class _FailingTaskService:
    def wait(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated facade polling failure")


class ActivityStoreSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stores: list[ActivityStore] = []

    def tearDown(self) -> None:
        for store in reversed(self.stores):
            store.close(timeout=2.0)
        self.temporary.cleanup()

    def _store(self, **options: object) -> ActivityStore:
        store = ActivityStore(self.root / "config-home", **options)  # type: ignore[arg-type]
        self.stores.append(store)
        return store

    def _database_bytes(self, config_home: Path | None = None) -> bytes:
        home = config_home or self.root / "config-home"
        payload = bytearray()
        for path in sorted(home.glob("activity.sqlite3*")):
            if path.is_file():
                payload.extend(path.read_bytes())
        return bytes(payload)

    def test_non_owner_observer_does_not_interrupt_a_live_job(self) -> None:
        owner = self._store(recover_interrupted=True)
        self.assertTrue(
            owner.record_job(
                JobHistoryRecord(
                    "live-job",
                    "auto_tag",
                    "running",
                    submitted_at="2026-07-19T08:00:00+00:00",
                    started_at="2026-07-19T08:00:01+00:00",
                )
            )
        )
        self.assertTrue(owner.flush())

        # This models a second/reconnecting desktop process while the backend
        # owning backend.lock is still working.
        observer = self._store(recover_interrupted=False)
        observed = observer.list_job_history()["items"][0]
        self.assertEqual(observed["status"], "running")
        self.assertEqual(observer.recovered_jobs, 0)
        self.assertEqual(
            observer.list_operation_logs(category="backend", limit=20)["total_count"],
            0,
        )

        # Once no old backend can still own its process lock, the next backend
        # owner is allowed to perform crash recovery.
        observer.close()
        owner.close()
        recovery_owner = self._store(recover_interrupted=True)
        recovered = recovery_owner.list_job_history()["items"][0]
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovery_owner.recovered_jobs, 1)

    def test_stale_active_snapshot_cannot_regress_a_terminal_job(self) -> None:
        terminal_writer = self._store(recover_interrupted=False)
        stale_writer = self._store(recover_interrupted=False)
        self.assertTrue(
            terminal_writer.record_job(
                JobHistoryRecord(
                    "terminal-job",
                    "index",
                    "succeeded",
                    processed=50,
                    total=50,
                    progress=1,
                    finished_at="2026-07-19T08:01:00+00:00",
                )
            )
        )
        self.assertTrue(terminal_writer.flush())

        # Separate WAL writers can finish queued writes in a different order.
        # A late running snapshot must not make completed work active again.
        self.assertTrue(
            stale_writer.record_job(
                JobHistoryRecord(
                    "terminal-job",
                    "index",
                    "running",
                    processed=49,
                    total=50,
                    progress=0.98,
                )
            )
        )
        self.assertTrue(stale_writer.flush())
        item = terminal_writer.list_job_history()["items"][0]
        self.assertEqual(item["status"], "succeeded")
        self.assertEqual(item["processed"], 50)
        self.assertIsNotNone(item["finished_at"])

    def test_live_reconnection_can_correct_a_previous_interrupted_marker(self) -> None:
        recovery_writer = self._store(recover_interrupted=False)
        live_writer = self._store(recover_interrupted=False)
        self.assertTrue(
            recovery_writer.record_job(
                JobHistoryRecord(
                    "reconnected-job",
                    "auto_tag",
                    "interrupted",
                    submitted_at="2026-07-19T08:00:00+00:00",
                    finished_at="2026-07-19T08:01:00+00:00",
                    error_code="backend_restarted",
                )
            )
        )
        self.assertTrue(recovery_writer.flush())
        self.assertTrue(
            live_writer.record_job(
                JobHistoryRecord(
                    "reconnected-job",
                    "auto_tag",
                    "running",
                    submitted_at="2026-07-19T08:00:00+00:00",
                    started_at="2026-07-19T08:00:01+00:00",
                    processed=3,
                    total=10,
                )
            )
        )
        self.assertTrue(live_writer.flush())
        item = recovery_writer.list_job_history()["items"][0]
        self.assertEqual(item["status"], "running")
        self.assertIsNone(item["finished_at"])
        self.assertIsNone(item["error_code"])

    def test_backend_terminal_snapshot_wins_over_observer_in_both_orders(self) -> None:
        for observer_first in (False, True):
            with self.subTest(observer_first=observer_first):
                home = self.root / f"authority-{int(observer_first)}"
                backend = ActivityStore(home, recover_interrupted=False)
                observer = ActivityStore(home, recover_interrupted=False)
                self.stores.extend((backend, observer))
                succeeded = JobHistoryRecord(
                    "authority-job",
                    "index",
                    "succeeded",
                    processed=10,
                    total=10,
                    progress=1,
                    finished_at="2026-07-19T08:01:00+00:00",
                )
                synthesized_failure = JobHistoryRecord(
                    "authority-job",
                    "index",
                    "failed",
                    processed=9,
                    total=10,
                    progress=0.9,
                    finished_at="2026-07-19T08:01:01+00:00",
                    error_code="job_failed",
                    error_message="facade polling failed",
                )
                writes = (
                    (
                        observer,
                        observer.record_observed_job,
                        synthesized_failure,
                    ),
                    (backend, backend.record_job, succeeded),
                )
                if not observer_first:
                    writes = tuple(reversed(writes))
                for store, writer, record in writes:
                    self.assertTrue(writer(record))
                    self.assertTrue(store.flush())

                item = backend.list_job_history()["items"][0]
                self.assertEqual(item["status"], "succeeded")
                self.assertEqual(item["processed"], 10)
                self.assertEqual(item["total"], 10)
                self.assertIsNone(item["error_code"])

    def test_facade_poll_failure_cannot_overwrite_backend_success(self) -> None:
        config_home = self.root / "poll-authority"
        config_path = config_home / "config.json"
        image_root = self.root / "poll-images"
        workspace = self.root / "poll-workspace"
        results = self.root / "poll-results"
        for path in (image_root, workspace, results):
            path.mkdir(parents=True)
        snapshot = DesktopConfigurationService(config_path).create_initial(
            image_root,
            workspace_directory=workspace,
            results_directory=results,
        )
        library_id = snapshot.configuration.default_library_id
        store = ActivityStore(config_home, recover_interrupted=False)
        self.stores.append(store)
        job_id = "b" * 32
        self.assertTrue(
            store.record_job(
                JobHistoryRecord(
                    job_id,
                    "index",
                    "succeeded",
                    library_id=library_id,
                    processed=10,
                    total=10,
                    progress=1,
                    finished_at="2026-07-19T08:01:00+00:00",
                )
            )
        )
        self.assertTrue(store.flush())

        submitted_job = {
            "id": job_id,
            "command": "index",
            "params": {"library_id": library_id},
            "status": "running",
            "progress": {"processed": 9, "total": 10, "percent": 90},
        }
        submission = SubmittedLibraryTask(
            command="index",
            job_id=job_id,
            params={"library_id": library_id},
            submitted_job=submitted_job,
        )
        facade = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            activity_store=store,
        )
        facade._task_service = _FailingTaskService()  # type: ignore[assignment]
        facade._tasks[job_id] = _TaskOperation(
            operation_id=job_id,
            task_type="index",
            library_id=library_id,
            submitted_at=0.0,
            submission=submission,
        )
        try:
            facade._wait_task_worker(job_id)
            self.assertEqual(facade._tasks[job_id].status, "failed")
            self.assertTrue(store.flush())
            item = store.list_job_history()["items"][0]
            self.assertEqual(item["status"], "succeeded")
            self.assertEqual(item["processed"], 10)
            self.assertEqual(item["total"], 10)
            self.assertIsNone(item["error_code"])
            logs = store.list_operation_logs(job_id=job_id, limit=20)
            self.assertTrue(any(log["event"] == "job_failed" for log in logs["items"]))
        finally:
            facade.close(force=True)

    def test_activity_read_mints_fresh_image_urls_without_persisting_them(self) -> None:
        config_home = self.root / "activity-image-view"
        config_path = config_home / "config.json"
        image_root = self.root / "activity-image-root"
        workspace = self.root / "activity-image-workspace"
        results = self.root / "activity-image-results"
        relative_path = "角色/broken.jpg"
        source = image_root / "角色" / "broken.jpg"
        source.parent.mkdir(parents=True)
        workspace.mkdir()
        results.mkdir()
        Image.new("RGB", (40, 60), (10, 20, 30)).save(source)
        snapshot = DesktopConfigurationService(config_path).create_initial(
            image_root,
            workspace_directory=workspace,
            results_directory=results,
        )
        library_id = snapshot.configuration.default_library_id
        store = ActivityStore(config_home, recover_interrupted=False)
        self.stores.append(store)
        self.assertTrue(
            store.log(
                level="warning",
                category="image_failure",
                event="image_failed",
                source="backend",
                message="decode failed",
                library_id=library_id,
                job_id="c" * 32,
                details={
                    "relative_path": relative_path,
                    "thumbnail_url": "api/image/stale?variant=thumbnail",
                    "image_url": "api/image/stale?variant=preview",
                },
            )
        )
        self.assertTrue(store.flush())
        raw_before_read = self._database_bytes(config_home)
        self.assertNotIn(b"api/image/stale", raw_before_read)

        facade = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            activity_store=store,
        )
        try:
            page = facade.activity_logs(
                category="image_failure",
                job_id="c" * 32,
                limit=10,
            )
            item = page["items"][0]
            self.assertEqual(item["details"]["relative_path"], relative_path)
            self.assertNotIn("thumbnail_url", item["details"])
            self.assertNotIn("image_url", item["details"])
            self.assertRegex(
                item["thumbnail_url"],
                r"^api/image/[A-Za-z0-9_-]+\?variant=thumbnail$",
            )
            self.assertRegex(
                item["image_url"],
                r"^api/image/[A-Za-z0-9_-]+\?variant=preview$",
            )
        finally:
            facade.close(force=True)

    def test_warning_events_take_priority_over_job_snapshots(self) -> None:
        store = self._store(
            queue_capacity=2,
            write_batch_size=2,
            auto_start=False,
            recover_interrupted=False,
        )
        for job_id in ("job-one", "job-two"):
            self.assertTrue(
                store.record_job(JobHistoryRecord(job_id, "index", "queued"))
            )
        self.assertTrue(
            store.log(
                level="warning",
                category="image_failure",
                event="important_failure",
                source="backend",
                message="must survive queue pressure",
            )
        )
        store.start()
        self.assertTrue(store.flush())
        events = {
            item["event"] for item in store.list_operation_logs(limit=20)["items"]
        }
        self.assertIn("important_failure", events)
        self.assertIn("activity_queue_dropped", events)
        self.assertEqual(store.stats()["dropped_jobs"], 1)

    def test_runtime_redactions_cover_rotated_credentials_and_generic_paths(
        self,
    ) -> None:
        store = self._store(recover_interrupted=False)
        secret = "DASHSCOPE_PRIVATE_VALUE_4A7F2D91"
        store.add_redactions(secret)
        self.assertTrue(
            store.log(
                level="error",
                category="frontend",
                event="window_error",
                source="webview",
                message=(
                    f"unexpected value {secret} at /archive/private/photos/a.jpg "
                    "and /private-file; URL https://example.test/v1/jobs"
                ),
                details={
                    "note": secret,
                    "location": "/custom/root/photo.jpg",
                    "relative_path": "角色（测试）/图片.jpg",
                },
            )
        )
        self.assertTrue(store.flush())
        item = store.list_operation_logs()["items"][0]
        rendered = repr(item)
        self.assertNotIn(secret, rendered)
        self.assertNotIn("/archive/private", rendered)
        self.assertNotIn("/custom/root", rendered)
        self.assertNotIn("/private-file", rendered)
        self.assertIn("https://example.test/v1/jobs", rendered)
        self.assertEqual(item["details"]["relative_path"], "角色（测试）/图片.jpg")
        raw = self._database_bytes()
        self.assertNotIn(secret.encode("utf-8"), raw)
        self.assertNotIn(b"/archive/private", raw)

    def test_backend_registers_new_credentials_before_job_history_can_log_them(
        self,
    ) -> None:
        store = self._store(recover_interrupted=False)
        manager = object.__new__(BackendJobManager)
        manager._activity = store
        manager._credentials = RuntimeCredentials()
        secret = "DASHSCOPE_BACKEND_PRIVATE_90AE21C4"

        configured = manager.configure_credentials({"dashscope_api_key": secret})
        self.assertTrue(configured["credentials_configured"])
        self.assertTrue(
            store.record_job(
                JobHistoryRecord(
                    "backend-secret-job",
                    "auto_tag",
                    "failed",
                    error_code="provider_rejected",
                    error_message=f"provider rejected {secret}",
                )
            )
        )
        self.assertTrue(store.flush())
        item = store.list_job_history()["items"][0]
        self.assertNotIn(secret, repr(item))
        self.assertNotIn(secret.encode("utf-8"), self._database_bytes())

    def test_server_close_stops_manager_before_releasing_instance_lock(self) -> None:
        calls: list[str] = []
        server = object.__new__(BackendHTTPServer)
        server.manager = _OrderedClose("manager", calls)  # type: ignore[assignment]
        server.socket = _OrderedClose("socket", calls)  # type: ignore[assignment]
        server._instance_lock = _OrderedClose(  # type: ignore[assignment]
            "instance_lock", calls
        )

        server.server_close()
        server.server_close()
        self.assertEqual(calls, ["manager", "socket", "instance_lock"])

    def test_facade_is_a_non_recovery_observer_and_does_not_own_injected_store(
        self,
    ) -> None:
        config_home = self.root / "facade-config"
        config_path = config_home / "config.json"
        image_root = self.root / "images"
        workspace = self.root / "workspace"
        results = self.root / "results"
        for path in (image_root, workspace, results):
            path.mkdir(parents=True)
        DesktopConfigurationService(config_path).create_initial(
            image_root,
            workspace_directory=workspace,
            results_directory=results,
        )

        seed = ActivityStore(config_home, recover_interrupted=False)
        self.stores.append(seed)
        self.assertTrue(
            seed.record_job(JobHistoryRecord("live-facade-job", "index", "running"))
        )
        self.assertTrue(seed.flush())
        seed.close()

        facade = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
        )
        rotated_secret = "DASHSCOPE_ROTATED_PRIVATE_81C34F70"
        try:
            item = facade._activity.list_job_history()["items"][0]
            self.assertEqual(item["status"], "running")
            self.assertFalse(facade._activity.stats()["recover_interrupted_on_start"])
            facade.save_credentials({"api_key": rotated_secret})
            # This simulates a later exception which embeds the newly entered
            # key without a helpful ``api_key=`` label.
            gateway = GatewayServer(facade, token="g" * 32)
            diagnostic = gateway._record_frontend_diagnostic(
                {
                    "event": "window_error",
                    "level": "error",
                    "message": f"provider rejected {rotated_secret}",
                }
            )
            self.assertTrue(diagnostic["persisted"])
            diagnostic_path = Path(str(diagnostic["log_file"]))
            self.assertNotIn(
                rotated_secret.encode("utf-8"), diagnostic_path.read_bytes()
            )
            self.assertTrue(
                facade.record_frontend_activity(
                    {
                        "event": "window_error",
                        "level": "error",
                        "message": f"provider rejected {rotated_secret}",
                    }
                )
            )
        finally:
            facade.close(force=True)
        self.assertNotIn(
            rotated_secret.encode("utf-8"), self._database_bytes(config_home)
        )

        shared = ActivityStore(config_home, recover_interrupted=False)
        self.stores.append(shared)
        injected = PreviewFacade(
            config_path,
            backend_host=_IdleHost(),  # type: ignore[arg-type]
            credential_store=SessionCredentialStore(),
            activity_store=shared,
        )
        injected.close(force=True)
        self.assertFalse(shared.stats()["closed"])
        self.assertTrue(
            shared.log(
                level="info",
                category="test",
                event="still_open",
                source="unit_test",
                message="external ownership preserved",
            )
        )


if __name__ == "__main__":
    unittest.main()
