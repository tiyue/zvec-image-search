from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from image_vector_service.backend_server import (
    BackendJobManager,
    create_backend_server,
)
from image_vector_service.config import ServiceConfig
from image_vector_service.library_config import LibraryCatalog, LibraryDefinition

_TERMINAL = {"succeeded", "partial", "needs_attention", "failed", "cancelled"}


class _SchedulingService:
    def __init__(
        self,
        progress: Any,
        cancel_check: Any,
        calls: list[str],
        calls_lock: threading.Lock,
        blocker_started: threading.Event,
        blocker_release: threading.Event,
    ) -> None:
        self.progress = progress
        self.cancel_check = cancel_check
        self.calls = calls
        self.calls_lock = calls_lock
        self.blocker_started = blocker_started
        self.blocker_release = blocker_release
        self.owner_thread = threading.get_ident()
        self.call_threads: list[int] = []
        self.close_thread: int | None = None
        self.optimize_adapter: Any = None
        self.maintenance_pending = False
        self.maintenance_wait_seconds: float | None = None
        self.maintenance_calls = 0
        self.maintenance_started = threading.Event()
        self.maintenance_release = threading.Event()
        self.maintenance_release.set()

    def _record(self, value: str) -> dict[str, Any]:
        self.cancel_check()
        with self.calls_lock:
            self.calls.append(value)
            self.call_threads.append(threading.get_ident())
        self.progress("Processed 1/1 item.")
        return {"operation": value}

    def index_folder(self, folder: str, **_kwargs: Any) -> dict[str, Any]:
        result = self._record(f"index:{folder}")
        if folder == "block":
            self.blocker_started.set()
            while not self.blocker_release.wait(0.01):
                self.cancel_check()
        return result

    def sync_folder(self, folder: str, **_kwargs: Any) -> dict[str, Any]:
        result = self._record(f"sync:{folder}")
        if folder == "block":
            self.blocker_started.set()
            while not self.blocker_release.wait(0.01):
                self.cancel_check()
        return result

    def auto_tag_images(self, **_kwargs: Any) -> dict[str, Any]:
        result = self._record("auto-tag")
        self.blocker_started.set()
        while not self.blocker_release.wait(0.01):
            self.cancel_check()
        return result

    def index_and_auto_tag_folder(self, folder: str, **_kwargs: Any) -> dict[str, Any]:
        result = self._record(f"index-and-auto-tag:{folder}")
        if folder == "block":
            self.blocker_started.set()
            while not self.blocker_release.wait(0.01):
                self.cancel_check()
        return result

    def search_by_text(self, text: str, **_kwargs: Any) -> dict[str, Any]:
        return self._record(f"search:{text}")

    def stats(self) -> dict[str, Any]:
        return self._record("stats")

    def configure_optimize_runtime(
        self,
        adapter: Any,
        *,
        externally_managed: bool,
    ) -> None:
        self.optimize_adapter = adapter
        self.externally_managed = externally_managed

    def has_pending_optimize_maintenance(self) -> bool:
        return self.maintenance_pending

    def optimize_maintenance_wait_seconds(self) -> float | None:
        if not self.maintenance_pending:
            return None
        return (
            0.0
            if self.maintenance_wait_seconds is None
            else (self.maintenance_wait_seconds)
        )

    def run_idle_maintenance(self) -> dict[str, Any]:
        self.maintenance_calls += 1
        self.maintenance_started.set()
        self.maintenance_release.wait(2)
        self.maintenance_pending = False
        return {"status": "succeeded"}

    def close(self) -> None:
        self.close_thread = threading.get_ident()


class BackendSchedulerBackpressureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="zvec_backend_scheduler_test_"
        )
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.results = self.root / "results"
        self.query_root = self.root / "query"
        for directory in (self.workspace, self.results, self.query_root):
            directory.mkdir(parents=True)
        library = LibraryDefinition(
            library_id="library-a",
            name="Library A",
            image_root=None,
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
        self.calls: list[str] = []
        self.calls_lock = threading.Lock()
        self.blocker_started = threading.Event()
        self.blocker_release = threading.Event()
        self.services: list[_SchedulingService] = []
        self.managers: list[BackendJobManager] = []

    def tearDown(self) -> None:
        self.blocker_release.set()
        for manager in reversed(self.managers):
            manager.close()
        self.temporary.cleanup()

    def _factory(
        self,
        _library: LibraryDefinition,
        progress: Any,
        cancel_check: Any,
    ) -> _SchedulingService:
        service = _SchedulingService(
            progress,
            cancel_check,
            self.calls,
            self.calls_lock,
            self.blocker_started,
            self.blocker_release,
        )
        self.services.append(service)
        return service

    def _manager(
        self, capacity: int, *, cooperative_batch_size: int | None = None
    ) -> BackendJobManager:
        manager = BackendJobManager(
            instance_id=f"scheduler-test-{len(self.managers)}",
            config_fingerprint="b" * 64,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=self._factory,
            library_queue_capacity=capacity,
            cooperative_batch_size=cooperative_batch_size,
        )
        self.managers.append(manager)
        manager.start()
        deadline = time.monotonic() + 2
        while not self.services and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.services, "library worker did not initialize")
        return manager

    def _wait(self, manager: BackendJobManager, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 5
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest = manager.get(job_id)
            if latest["status"] in _TERMINAL:
                return latest
            time.sleep(0.005)
        self.fail(f"job did not finish: {job_id}; latest={latest}")

    def _start_blocker(self, manager: BackendJobManager) -> dict[str, Any]:
        blocker = manager.submit({"command": "index", "params": {"folder": "block"}})
        self.assertTrue(self.blocker_started.wait(1), "blocker did not start")
        return blocker

    def test_high_and_interactive_jobs_run_before_queued_batch_fifo(self) -> None:
        manager = self._manager(capacity=8)
        blocker = self._start_blocker(manager)
        low_first = manager.submit({"command": "index", "params": {"folder": "low-1"}})
        low_second = manager.submit({"command": "index", "params": {"folder": "low-2"}})
        interactive = manager.submit({"command": "stats", "params": {}})
        high = manager.submit({"command": "search", "params": {"text": "urgent"}})

        self.assertEqual(high["queue_priority"], "high")
        self.assertEqual(high["queue_position"], 1)
        self.assertEqual(manager.get(interactive["id"])["queue_position"], 2)
        self.assertEqual(manager.get(low_first["id"])["queue_position"], 3)
        self.assertEqual(manager.get(low_second["id"])["queue_position"], 4)

        self.blocker_release.set()
        for job in (blocker, low_first, low_second, interactive, high):
            self.assertEqual(self._wait(manager, job["id"])["status"], "succeeded")

        self.assertEqual(
            self.calls,
            ["index:block", "search:urgent", "stats", "index:low-1", "index:low-2"],
        )
        service = self.services[0]
        self.assertTrue(service.call_threads)
        self.assertEqual(set(service.call_threads), {service.owner_thread})

    def test_policy_snapshot_uses_runtime_queue_overrides(self) -> None:
        manager = self._manager(capacity=3, cooperative_batch_size=7)

        policy = manager.version_info()["large_library_policy"]

        self.assertEqual(policy["queue"]["capacity_per_library"], 3)
        self.assertEqual(policy["queue"]["cooperative_checkpoint_items"], 7)

    def test_job_is_completed_before_idle_maintenance_runs(self) -> None:
        manager = self._manager(capacity=4)
        service = self.services[0]
        service.maintenance_pending = True
        service.maintenance_release.clear()

        job = manager.submit({"command": "stats", "params": {}})

        self.assertTrue(service.maintenance_started.wait(3))
        self.assertEqual(manager.get(job["id"])["status"], "succeeded")
        self.assertEqual(service.maintenance_calls, 1)
        service.maintenance_release.set()

    def test_new_search_preempts_the_idle_maintenance_grace_period(self) -> None:
        manager = self._manager(capacity=4)
        service = self.services[0]
        service.maintenance_pending = True

        stats = manager.submit({"command": "stats", "params": {}})
        self.assertEqual(self._wait(manager, stats["id"])["status"], "succeeded")
        search = manager.submit(
            {"command": "search", "params": {"text": "arrived-during-grace"}}
        )

        self.assertEqual(self._wait(manager, search["id"])["status"], "succeeded")
        self.assertEqual(self.calls[:2], ["stats", "search:arrived-during-grace"])
        self.assertFalse(service.maintenance_started.is_set())

    def test_read_only_jobs_only_check_the_in_memory_pending_hint(self) -> None:
        manager = self._manager(capacity=4)
        service = self.services[0]

        jobs = [manager.submit({"command": "stats", "params": {}}) for _ in range(3)]
        for job in jobs:
            self.assertEqual(self._wait(manager, job["id"])["status"], "succeeded")

        self.assertEqual(service.maintenance_calls, 0)
        self.assertIsNotNone(service.optimize_adapter)

    def test_idle_worker_wakes_when_small_batch_reaches_max_interval(self) -> None:
        manager = self._manager(capacity=4)
        service = self.services[0]
        service.maintenance_pending = True
        service.maintenance_wait_seconds = 0.05

        job = manager.submit({"command": "stats", "params": {}})

        self.assertEqual(self._wait(manager, job["id"])["status"], "succeeded")
        self.assertTrue(service.maintenance_started.wait(1))
        self.assertEqual(service.maintenance_calls, 1)

    def test_weighted_priority_does_not_starve_waiting_batch(self) -> None:
        manager = self._manager(capacity=12)
        blocker = self._start_blocker(manager)
        batch = manager.submit(
            {"command": "index", "params": {"folder": "waiting-batch"}}
        )
        high_jobs = [
            manager.submit({"command": "search", "params": {"text": f"high-{index}"}})
            for index in range(8)
        ]

        self.blocker_release.set()
        for job in (blocker, batch, *high_jobs):
            self.assertEqual(self._wait(manager, job["id"])["status"], "succeeded")

        after_blocker = self.calls[1:]
        batch_index = after_blocker.index("index:waiting-batch")
        self.assertLessEqual(batch_index, 4, after_blocker)
        self.assertEqual(
            after_blocker[:4], [f"search:high-{index}" for index in range(4)]
        )

    def test_index_yields_to_search_without_concurrent_collection_access(self) -> None:
        manager = self._manager(capacity=8, cooperative_batch_size=1)
        blocker = self._start_blocker(manager)
        interactive = manager.submit({"command": "stats", "params": {}})
        urgent = manager.submit(
            {"command": "search", "params": {"text": "during-index"}}
        )

        urgent_result = self._wait(manager, urgent["id"])
        self.assertEqual(urgent_result["status"], "succeeded")
        self.assertEqual(manager.get(blocker["id"])["status"], "running")
        # Mutating/default-interactive work remains queued until the partially
        # completed index returns; only explicitly high/read-only calls yield.
        self.assertEqual(manager.get(interactive["id"])["status"], "queued")

        self.blocker_release.set()
        self.assertEqual(self._wait(manager, blocker["id"])["status"], "succeeded")
        self.assertEqual(self._wait(manager, interactive["id"])["status"], "succeeded")
        self.assertEqual(
            self.calls,
            ["index:block", "search:during-index", "stats"],
        )
        service = self.services[0]
        self.assertEqual(set(service.call_threads), {service.owner_thread})

    def test_sync_yields_to_search_at_the_same_owner_thread_checkpoint(self) -> None:
        manager = self._manager(capacity=4, cooperative_batch_size=1)
        blocker = manager.submit({"command": "sync", "params": {"folder": "block"}})
        self.assertTrue(self.blocker_started.wait(1), "sync blocker did not start")
        urgent = manager.submit(
            {"command": "search", "params": {"text": "during-sync"}}
        )

        self.assertEqual(self._wait(manager, urgent["id"])["status"], "succeeded")
        self.assertEqual(manager.get(blocker["id"])["status"], "running")
        self.blocker_release.set()
        self.assertEqual(self._wait(manager, blocker["id"])["status"], "succeeded")
        self.assertEqual(self.calls, ["sync:block", "search:during-sync"])
        service = self.services[0]
        self.assertEqual(set(service.call_threads), {service.owner_thread})

    def test_auto_tag_yields_without_bypassing_external_processing_contract(
        self,
    ) -> None:
        manager = self._manager(capacity=4, cooperative_batch_size=1)
        blocker = manager.submit(
            {
                "command": "auto_tag",
                "params": {
                    "scope": "untagged",
                    "max_images": 200,
                    "external_processing_confirmed": True,
                },
            }
        )
        self.assertTrue(self.blocker_started.wait(1), "auto-tag blocker did not start")
        urgent = manager.submit(
            {"command": "search", "params": {"text": "during-auto-tag"}}
        )

        self.assertEqual(self._wait(manager, urgent["id"])["status"], "succeeded")
        self.assertEqual(manager.get(blocker["id"])["status"], "running")
        self.blocker_release.set()
        self.assertEqual(self._wait(manager, blocker["id"])["status"], "succeeded")
        self.assertEqual(self.calls, ["auto-tag", "search:during-auto-tag"])

    def test_queued_cancellation_removes_work_and_reclaims_capacity(self) -> None:
        manager = self._manager(capacity=1)
        blocker = self._start_blocker(manager)
        queued = manager.submit({"command": "stats", "params": {}})
        self.assertEqual(queued["queue_position"], 1)

        cancelled = manager.cancel(queued["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        replacement = manager.submit(
            {"command": "search", "params": {"text": "replacement"}}
        )
        self.assertEqual(replacement["queue_position"], 1)

        self.blocker_release.set()
        self.assertEqual(self._wait(manager, blocker["id"])["status"], "succeeded")
        self.assertEqual(self._wait(manager, queued["id"])["status"], "cancelled")
        self.assertEqual(self._wait(manager, replacement["id"])["status"], "succeeded")
        self.assertNotIn("stats", self.calls)

    def test_close_cancels_pending_jobs_without_deadlock_or_execution(self) -> None:
        manager = self._manager(capacity=4)
        blocker = self._start_blocker(manager)
        queued_stats = manager.submit({"command": "stats", "params": {}})
        queued_batch = manager.submit(
            {"command": "index", "params": {"folder": "never-run"}}
        )

        close_thread = threading.Thread(target=manager.close, daemon=True)
        close_thread.start()
        close_thread.join(timeout=2)
        self.assertFalse(close_thread.is_alive(), "backend close deadlocked")
        self.assertEqual(manager.get(blocker["id"])["status"], "cancelled")
        self.assertEqual(manager.get(queued_stats["id"])["status"], "cancelled")
        self.assertEqual(manager.get(queued_batch["id"])["status"], "cancelled")
        self.assertEqual(self.calls, ["index:block"])
        self.assertEqual(self.services[0].close_thread, self.services[0].owner_thread)

    def test_worker_startup_failure_fails_pending_job_and_close_does_not_deadlock(
        self,
    ) -> None:
        factory_started = threading.Event()
        allow_failure = threading.Event()

        def failing_factory(
            _library: LibraryDefinition,
            _progress: Any,
            _cancel_check: Any,
        ) -> _SchedulingService:
            factory_started.set()
            allow_failure.wait(1)
            raise RuntimeError("injected startup failure")

        manager = BackendJobManager(
            instance_id="scheduler-startup-failure",
            config_fingerprint="d" * 64,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=failing_factory,
            library_queue_capacity=2,
        )
        self.managers.append(manager)
        manager.start()
        self.assertTrue(factory_started.wait(1))
        pending = manager.submit({"command": "stats", "params": {}})
        allow_failure.set()

        failed = self._wait(manager, pending["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("startup failure", failed["error"]["message"])
        close_thread = threading.Thread(target=manager.close, daemon=True)
        close_thread.start()
        close_thread.join(timeout=2)
        self.assertFalse(
            close_thread.is_alive(), "startup-failed worker close deadlocked"
        )

    def test_http_queue_full_returns_429_retry_after_and_capacity_details(self) -> None:
        token = "scheduler-backpressure-token"
        lock_path = self.root / "backend.lock"
        server, manager = create_backend_server(
            host="127.0.0.1",
            port=0,
            token=token,
            instance_id="scheduler-http-test",
            config_fingerprint="c" * 64,
            instance_lock_path=lock_path,
            config=self.config,
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=self._factory,
            library_queue_capacity=1,
        )
        self.managers.append(manager)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        thread.start()

        def submit(
            command: str, params: dict[str, Any]
        ) -> tuple[int, dict[str, Any], str | None]:
            connection = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=3
            )
            body = json.dumps({"command": command, "params": params}).encode()
            connection.request(
                "POST",
                "/v1/jobs",
                body=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            retry_after = response.getheader("Retry-After")
            connection.close()
            return response.status, payload, retry_after

        try:
            status, blocker_payload, _retry = submit("index", {"folder": "block"})
            self.assertEqual(status, 202, blocker_payload)
            self.assertTrue(self.blocker_started.wait(1))
            status, queued_payload, _retry = submit("index", {"folder": "queued"})
            self.assertEqual(status, 202, queued_payload)

            status, rejected, retry_after = submit("search", {"text": "urgent"})
            self.assertEqual(status, 429, rejected)
            self.assertEqual(retry_after, "1")
            self.assertEqual(rejected["error"]["code"], "queue_full")
            self.assertEqual(rejected["error"]["details"]["queue_depth"], 1)
            self.assertEqual(rejected["error"]["details"]["queue_capacity"], 1)
            self.assertEqual(rejected["error"]["details"]["retry_after_seconds"], 1)
            self.assertEqual(manager.list_jobs(limit=20)["total_count"], 2)
            self.assertEqual(len(manager._job_futures), 2)
        finally:
            self.blocker_release.set()
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
