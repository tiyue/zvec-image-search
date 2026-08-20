from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from image_vector_service.activity_store import ActivityStore
from image_vector_service.backend_instance_lock import (
    BackendInstanceLock,
    BackendInstanceLockError,
)
from image_vector_service.backend_server import BackendJobManager, create_backend_server
from image_vector_service.config import ServiceConfig
from image_vector_service.library_config import LibraryCatalog, LibraryDefinition

_TERMINAL_STATUSES = {"succeeded", "partial", "needs_attention", "failed", "cancelled"}


class _ScriptedService:
    """Small in-memory service used to exercise the real backend job lifecycle."""

    def __init__(
        self,
        progress: Any,
        cancel_check: Any,
        *,
        block_index: bool = False,
        fail_first_index: bool = False,
    ) -> None:
        self._progress = progress
        self._cancel_check = cancel_check
        self._block_index = block_index
        self._fail_first_index = fail_first_index
        self._index_calls = 0
        self.index_started = threading.Event()
        self.release_index = threading.Event()

    def index_folder(self, _folder: str, **_kwargs: Any) -> dict[str, int]:
        self._index_calls += 1
        self._cancel_check()
        self._progress("Embedded 2/3 unique images.")
        self.index_started.set()
        if self._block_index and not self.release_index.wait(timeout=5):
            raise TimeoutError("test did not release the blocked index operation")
        self._cancel_check()
        if self._fail_first_index and self._index_calls == 1:
            raise RuntimeError("simulated per-job index failure")
        return {"processed": 3, "total": 3, "failed": 0}

    def search_by_text(self, text: str, **_kwargs: Any) -> dict[str, Any]:
        self._cancel_check()
        return {
            "query_type": "text",
            "text": text,
            "result_count": 0,
            "results": [],
        }

    def stats(self) -> dict[str, int]:
        self._cancel_check()
        return {"tracked_files": 0}

    def list_roots(self) -> list[dict[str, Any]]:
        self._cancel_check()
        return []

    def list_image_clusters(self, **params: Any) -> dict[str, Any]:
        self._cancel_check()
        return {
            "total": 1,
            "offset": params["offset"],
            "limit": params["limit"],
            "items": [],
        }

    def image_cluster_detail(
        self, cluster_id: str, *, offset: int, limit: int
    ) -> dict[str, Any]:
        self._cancel_check()
        return {
            "cluster": {"cluster_id": cluster_id, "member_count": 0},
            "members": [],
            "offset": offset,
            "limit": limit,
        }

    def close(self) -> None:
        return


class _CombinedProgressService(_ScriptedService):
    """Pause at each combined stage so live and durable progress can be inspected."""

    def __init__(self, progress: Any, cancel_check: Any) -> None:
        super().__init__(progress, cancel_check)
        self.index_complete = threading.Event()
        self.release_index = threading.Event()
        self.tag_started = threading.Event()
        self.release_tag_start = threading.Event()
        self.tag_complete = threading.Event()
        self.release_tag_complete = threading.Event()

    def index_and_auto_tag_folder(
        self,
        _folder: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self._cancel_check()
        self._progress("Scanning: synthetic library")
        self._progress("Found 4 valid images; skipped 0; invalid 0.")
        self._progress("Embedded 4/4 unique images.")
        self.index_complete.set()
        if not self.release_index.wait(timeout=5):
            raise TimeoutError("test did not release the completed index stage")

        self._cancel_check()
        self._progress("智能标注进行中 0/3")
        self.tag_started.set()
        if not self.release_tag_start.wait(timeout=5):
            raise TimeoutError("test did not release the started auto-tag stage")

        self._cancel_check()
        self._progress("Auto-tagged 3/3 unique images.")
        self.tag_complete.set()
        if not self.release_tag_complete.wait(timeout=5):
            raise TimeoutError("test did not release the completed auto-tag stage")

        return {
            "failed": 0,
            "needs_attention": False,
            "index": {"inserted": 4, "failed": 0},
            "auto_tag": {
                "processed": 3,
                "candidate_count": 3,
                "failed": 0,
            },
        }


class _WatcherCycleState:
    def root_path(self, root_id: str) -> str | None:
        return "D:\\images" if root_id == "root-1" else None

    def pending_change_snapshot(self, root_id: str) -> dict[str, int | str]:
        return {
            "root_id": root_id,
            "cutoff_sequence": 2,
            "total": 2,
            "created": 1,
            "modified": 1,
            "deleted": 0,
        }

    def count_pending_changes(self, _root_id: str) -> int:
        return 0


class _WatcherCycleService:
    def __init__(self, progress: Any, cancel_check: Any) -> None:
        self.progress = progress
        self.cancel_check = cancel_check
        self.state = _WatcherCycleState()

    def index_and_auto_tag_incremental(self, **_kwargs: Any) -> dict[str, Any]:
        self.cancel_check()
        return {
            "failed": 0,
            "needs_attention": False,
            "changes": {"processed": 2, "created": 1, "modified": 1, "deleted": 0},
            "index": {"inserted": 1, "updated": 1, "deleted": 0, "failed": 0},
            "auto_tag": {"processed": 1, "succeeded": 1, "failed": 0},
        }

    def close(self) -> None:
        return None


class BackendActivityIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="zvec_backend_activity_test_"
        )
        self.root = Path(self.temporary.name).resolve()
        self.config_home = self.root / "config-home"
        self.image_root = self.root / "images"
        self.workspace = self.root / "workspace"
        self.results = self.root / "results"
        self.query_root = self.root / "query"
        for directory in (
            self.image_root,
            self.workspace,
            self.results,
            self.query_root,
        ):
            directory.mkdir(parents=True)
        library = LibraryDefinition(
            library_id="library-a",
            name="Library A",
            image_root=self.image_root,
            workspace=self.workspace,
        )
        self.catalog = LibraryCatalog(
            default_library_id=library.library_id,
            libraries=(library,),
            federated_results_directory=self.results,
        )
        self.config = ServiceConfig(
            workspace=self.root / "control",
            results_directory=self.results,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manager_with_service_options(
        self,
        activity: ActivityStore,
        *,
        block_index: bool = False,
        fail_first_index: bool = False,
    ) -> BackendJobManager:

        def factory(
            _library: LibraryDefinition,
            progress: Any,
            cancel_check: Any,
        ) -> _ScriptedService:
            service = _ScriptedService(
                progress,
                cancel_check,
                block_index=block_index,
                fail_first_index=fail_first_index,
            )
            return service

        manager = BackendJobManager(
            instance_id="activity-test-instance",
            config_fingerprint="a" * 64,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=factory,
            activity_store=activity,
        )
        return manager

    @staticmethod
    def _wait_for_job(
        manager: BackendJobManager,
        job_id: str,
        *,
        terminal: bool = True,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest = manager.get(job_id)
            if terminal and latest["status"] in _TERMINAL_STATUSES:
                return latest
            if not terminal and latest["status"] == "running":
                return latest
            time.sleep(0.01)
        raise AssertionError(f"job did not reach the expected state: {latest}")

    @staticmethod
    def _history_item(
        store: ActivityStore,
        job_id: str,
        *,
        expected_status: str | None = None,
        timeout: float = 3.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        latest: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            store.flush(timeout=1.0)
            page = store.list_job_history(limit=200)
            latest = next(
                (item for item in page["items"] if item["job_id"] == job_id),
                None,
            )
            if latest is not None and (
                expected_status is None or latest["status"] == expected_status
            ):
                return latest
            time.sleep(0.01)
        raise AssertionError(
            f"job history did not reach {expected_status!r}: {latest!r}"
        )

    def test_persists_queued_running_progress_and_terminal_snapshots(self) -> None:
        store = ActivityStore(self.config_home)
        service_holder: dict[str, _ScriptedService] = {}

        def factory(
            _library: LibraryDefinition,
            progress: Any,
            cancel_check: Any,
        ) -> _ScriptedService:
            service = _ScriptedService(progress, cancel_check, block_index=True)
            service_holder["service"] = service
            return service

        manager = BackendJobManager(
            instance_id="activity-lifecycle",
            config_fingerprint="b" * 64,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=factory,
            activity_store=store,
        )
        try:
            submitted = manager.submit({"command": "index", "params": {}})
            job_id = submitted["id"]
            queued = self._history_item(store, job_id, expected_status="queued")
            self.assertEqual(queued["processed"], 0)
            self.assertEqual(queued["total"], 0)

            manager.start()
            deadline = time.monotonic() + 3
            while "service" not in service_holder and time.monotonic() < deadline:
                time.sleep(0.01)
            active_service = service_holder["service"]
            self.assertTrue(active_service.index_started.wait(timeout=3))
            self._wait_for_job(manager, job_id, terminal=False)

            running = self._history_item(store, job_id, expected_status="running")
            self.assertEqual(running["processed"], 2)
            self.assertEqual(running["total"], 3)
            self.assertAlmostEqual(running["progress"], 2 / 3)
            self.assertIn("Embedded 2/3", running["message"])

            active_service.release_index.set()
            completed = self._wait_for_job(manager, job_id)
            self.assertEqual(completed["status"], "succeeded")
            terminal = self._history_item(store, job_id, expected_status="succeeded")
            # Live progress is retained while running, but the authoritative
            # terminal result must replace it so a completed task reads 3/3.
            self.assertEqual(terminal["processed"], 3)
            self.assertEqual(terminal["total"], 3)
            self.assertAlmostEqual(terminal["progress"], 1.0)
            self.assertIsNotNone(terminal["started_at"])
            self.assertIsNotNone(terminal["finished_at"])
        finally:
            cleanup_service = service_holder.get("service")
            if cleanup_service is not None:
                cleanup_service.release_index.set()
            manager.close()
            store.close()

    def test_watcher_cycle_is_a_durable_task_with_one_aggregate_log(self) -> None:
        store = ActivityStore(self.config_home)

        def factory(
            _library: LibraryDefinition,
            progress: Any,
            cancel_check: Any,
        ) -> _WatcherCycleService:
            return _WatcherCycleService(progress, cancel_check)

        manager = BackendJobManager(
            instance_id="activity-watcher-cycle",
            config_fingerprint="9" * 64,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=factory,
            activity_store=store,
        )
        try:
            manager.start()
            manager._submit_auto_index_cycle(self.catalog.enabled[0], "root-1")
            jobs = manager.list_jobs(limit=10)["jobs"]
            job_id = next(
                item["id"]
                for item in jobs
                if item["command"] == "auto_index_and_auto_tag"
            )
            completed = self._wait_for_job(manager, job_id)

            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["result"]["changes"]["processed"], 2)
            history = self._history_item(
                store, job_id, expected_status="succeeded"
            )
            self.assertEqual(history["task_type"], "auto_index_and_auto_tag")
            store.flush(timeout=1.0)
            logs = store.list_operation_logs(category="auto_index", limit=10)[
                "items"
            ]
            matching = [item for item in logs if item["job_id"] == job_id]
            self.assertEqual(len(matching), 1)
            self.assertIn("处理 2 个文件变化", matching[0]["message"])
            self.assertIn("新增 1 张", matching[0]["message"])
        finally:
            manager.close()
            store.close()

    def test_combined_progress_is_monotonic_across_index_and_auto_tag_stages(
        self,
    ) -> None:
        store = ActivityStore(self.config_home)
        service_holder: dict[str, _CombinedProgressService] = {}

        def factory(
            _library: LibraryDefinition,
            progress: Any,
            cancel_check: Any,
        ) -> _CombinedProgressService:
            service = _CombinedProgressService(progress, cancel_check)
            service_holder["service"] = service
            return service

        manager = BackendJobManager(
            instance_id="activity-combined-progress",
            config_fingerprint="c" * 64,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=factory,
            activity_store=store,
        )
        try:
            manager.start()
            submitted = manager.submit(
                {
                    "command": "index_and_auto_tag",
                    "params": {"external_processing_confirmed": True},
                }
            )
            job_id = submitted["id"]
            service = service_holder["service"]

            self.assertTrue(service.index_complete.wait(timeout=3))
            indexed = manager.get(job_id)["progress"]
            self.assertEqual(indexed["stage"], "indexing")
            self.assertEqual(indexed["current"], 4)
            self.assertEqual(indexed["total"], 4)
            self.assertEqual(indexed["percent"], 50.0)

            service.release_index.set()
            self.assertTrue(service.tag_started.wait(timeout=3))
            tag_started = manager.get(job_id)["progress"]
            self.assertEqual(tag_started["stage"], "auto_tagging")
            self.assertEqual(tag_started["current"], 0)
            self.assertEqual(tag_started["total"], 3)
            self.assertEqual(tag_started["percent"], 50.0)

            service.release_tag_start.set()
            self.assertTrue(service.tag_complete.wait(timeout=3))
            tag_complete = manager.get(job_id)["progress"]
            self.assertEqual(tag_complete["stage"], "auto_tagging")
            self.assertEqual(tag_complete["current"], 3)
            self.assertEqual(tag_complete["total"], 3)
            self.assertEqual(tag_complete["percent"], 99.0)

            service.release_tag_complete.set()
            completed = self._wait_for_job(manager, job_id)
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(completed["progress"]["percent"], 100.0)
            terminal = self._history_item(
                store,
                job_id,
                expected_status="succeeded",
            )
            self.assertEqual(terminal["processed"], 3)
            self.assertEqual(terminal["total"], 3)
            self.assertEqual(terminal["progress"], 1.0)
        finally:
            service = service_holder.get("service")
            if service is not None:
                service.release_index.set()
                service.release_tag_start.set()
                service.release_tag_complete.set()
            manager.close()
            store.close()

    def test_search_and_read_only_jobs_are_excluded_from_history(self) -> None:
        store = ActivityStore(self.config_home)
        service_holder: dict[str, _ScriptedService] = {}

        def factory(
            _library: LibraryDefinition,
            progress: Any,
            cancel_check: Any,
        ) -> _ScriptedService:
            service = _ScriptedService(progress, cancel_check)
            service_holder["service"] = service
            return service

        manager = BackendJobManager(
            instance_id="activity-read-only",
            config_fingerprint="c" * 64,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=factory,
            activity_store=store,
        )
        manager.start()
        try:
            jobs = [
                manager.submit({"command": "libraries", "params": {}}),
                manager.submit({"command": "stats", "params": {}}),
                manager.submit({"command": "roots", "params": {}}),
                manager.submit({"command": "search", "params": {"text": "portrait"}}),
                manager.submit(
                    {
                        "command": "cluster_list",
                        "params": {
                            "library_id": "library-a",
                            "offset": 0,
                            "limit": 10,
                            "cluster_type": "all",
                        },
                    }
                ),
                manager.submit(
                    {
                        "command": "cluster_detail",
                        "params": {
                            "library_id": "library-a",
                            "cluster_id": "cluster-1",
                            "offset": 0,
                            "limit": 10,
                        },
                    }
                ),
            ]
            for job in jobs:
                completed = self._wait_for_job(manager, job["id"])
                self.assertEqual(completed["status"], "succeeded", completed)

            self.assertTrue(store.flush(timeout=2.0))
            page = store.list_job_history(limit=200)
            persisted_ids = {item["job_id"] for item in page["items"]}
            self.assertTrue(persisted_ids.isdisjoint({job["id"] for job in jobs}))
            self.assertEqual(page["total_count"], 0)
        finally:
            manager.close()
            store.close()

    def test_image_failures_are_logged_without_a_connected_facade(self) -> None:
        store = ActivityStore(self.config_home)

        class FailureService(_ScriptedService):
            def index_folder(self, _folder: str, **_kwargs: Any) -> dict[str, Any]:
                self._cancel_check()
                return {
                    "processed": 1,
                    "total": 2,
                    "failed": 1,
                    "error_images": [
                        {
                            "name": "broken.jpg",
                            "relative_path": "角色/broken.jpg",
                            "source_path": str(self_root / "private" / "broken.jpg"),
                            "thumbnail_url": "api/image/stale?variant=thumbnail",
                            "reason": "decode failed",
                        }
                    ],
                }

        self_root = self.root

        def factory(
            _library: LibraryDefinition,
            progress: Any,
            cancel_check: Any,
        ) -> FailureService:
            return FailureService(progress, cancel_check)

        manager = BackendJobManager(
            instance_id="activity-offline-failure",
            config_fingerprint="e" * 64,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=factory,
            activity_store=store,
        )
        manager.start()
        try:
            submitted = manager.submit({"command": "index", "params": {}})
            completed = self._wait_for_job(manager, submitted["id"])
            self.assertEqual(completed["status"], "partial")
            self.assertTrue(store.flush(timeout=2.0))
            page = store.list_operation_logs(
                category="image_failure",
                job_id=submitted["id"],
                limit=20,
            )
            self.assertEqual(page["total_count"], 1)
            item = page["items"][0]
            self.assertEqual(item["details"]["relative_path"], "角色/broken.jpg")
            self.assertNotIn("source_path", item["details"])
            self.assertNotIn("thumbnail_url", item["details"])
            raw = b"".join(
                path.read_bytes()
                for path in self.config_home.glob("activity.sqlite3*")
                if path.is_file()
            )
            self.assertNotIn(b"api/image/stale", raw)
            self.assertNotIn(str(self_root).encode(), raw)
        finally:
            manager.close()
            store.close()

    def test_failed_job_does_not_stop_following_job_or_history_updates(self) -> None:
        store = ActivityStore(self.config_home)
        manager = self._manager_with_service_options(store, fail_first_index=True)
        manager.start()
        try:
            first = manager.submit({"command": "index", "params": {}})
            first_done = self._wait_for_job(manager, first["id"])
            self.assertEqual(first_done["status"], "failed")
            self.assertEqual(first_done["error"]["code"], "job_failed")

            second = manager.submit({"command": "index", "params": {}})
            second_done = self._wait_for_job(manager, second["id"])
            self.assertEqual(second_done["status"], "succeeded", second_done)

            first_history = self._history_item(
                store, first["id"], expected_status="failed"
            )
            second_history = self._history_item(
                store, second["id"], expected_status="succeeded"
            )
            self.assertEqual(first_history["error_code"], "job_failed")
            self.assertIn("simulated per-job", first_history["error_message"])
            self.assertIsNone(second_history["error_code"])
        finally:
            manager.close()
            store.close()

    def test_unavailable_activity_store_does_not_affect_primary_job(self) -> None:
        blocking_file = self.root / "not-a-directory"
        blocking_file.write_text("file", encoding="utf-8")
        store = ActivityStore(blocking_file / "config-home")
        self.assertFalse(store.available)

        manager = self._manager_with_service_options(store)
        manager.start()
        try:
            submitted = manager.submit({"command": "index", "params": {}})
            completed = self._wait_for_job(manager, submitted["id"])
            self.assertEqual(completed["status"], "succeeded", completed)
            self.assertEqual(completed["result"]["processed"], 3)
        finally:
            manager.close()
            store.close()

    def test_second_backend_lock_failure_does_not_interrupt_active_history(
        self,
    ) -> None:
        store = ActivityStore(self.config_home)
        service_holder: dict[str, _ScriptedService] = {}

        def first_factory(
            _library: LibraryDefinition,
            progress: Any,
            cancel_check: Any,
        ) -> _ScriptedService:
            service = _ScriptedService(progress, cancel_check, block_index=True)
            service_holder["service"] = service
            return service

        manager = BackendJobManager(
            instance_id="first-backend",
            config_fingerprint="d" * 64,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=first_factory,
            activity_store=store,
        )
        lock_path = self.root / "backend.lock"
        owner_lock = BackendInstanceLock(lock_path)
        owner_lock.acquire({"instance_id": "first-backend"})
        manager.start()
        try:
            submitted = manager.submit({"command": "index", "params": {}})
            job_id = submitted["id"]
            deadline = time.monotonic() + 3
            while "service" not in service_holder and time.monotonic() < deadline:
                time.sleep(0.01)
            active_service = service_holder["service"]
            self.assertTrue(active_service.index_started.wait(timeout=3))
            self._wait_for_job(manager, job_id, terminal=False)
            before = self._history_item(store, job_id, expected_status="running")

            with self.assertRaises(BackendInstanceLockError) as caught:
                create_backend_server(
                    host="127.0.0.1",
                    port=0,
                    token="second-token",
                    instance_id="second-backend",
                    instance_lock_path=lock_path,
                    config=self.config,
                    query_root=self.query_root,
                    library_catalog=self.catalog,
                    library_service_factory=first_factory,
                    activity_config_home=self.config_home,
                )
            self.assertEqual(caught.exception.code, "backend_already_running")

            after = self._history_item(store, job_id, expected_status="running")
            self.assertEqual(after["status"], "running")
            self.assertEqual(after["started_at"], before["started_at"])
            self.assertIsNone(after["finished_at"])
            self.assertNotEqual(after["error_code"], "backend_restarted")
        finally:
            cleanup_service = service_holder.get("service")
            if cleanup_service is not None:
                cleanup_service.release_index.set()
            manager.close()
            owner_lock.release()
            store.close()


if __name__ == "__main__":
    unittest.main()
