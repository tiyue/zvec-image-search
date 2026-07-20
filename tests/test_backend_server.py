from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest.mock import patch

import image_service
from image_vector_service.backend_instance_lock import (
    BackendInstanceLock,
    BackendInstanceLockError,
)
from image_vector_service.backend_server import create_backend_server
from image_vector_service.config import ServiceConfig
from image_vector_service.library_config import LibraryCatalog, LibraryDefinition
from image_vector_service.models import (
    ExportedHit,
    PreparedSearch,
    PreparedSearchCandidates,
    RankSource,
    ResolvedSearchHit,
    SearchHit,
    SearchReport,
)
from image_vector_service.result_exporter import RESULT_OWNERSHIP_MARKER
from image_vector_service.search_result_store import RESULT_STORE_FILENAME


class FakeReport:
    def __init__(self, operation: str, values: dict):
        self.operation = operation
        self.values = values

    def to_dict(self) -> dict:
        return {"operation": self.operation, **self.values}


class BackendInstanceLockProcessTest(unittest.TestCase):
    def test_lock_blocks_an_independent_process_and_recovers_after_exit(self):
        with tempfile.TemporaryDirectory(prefix="zvec_backend_lock_test_") as temporary:
            root = Path(temporary)
            lock_path = root / "backend.lock"
            ready_path = root / "ready"
            script = """
import sys
import time
from pathlib import Path
from image_vector_service.backend_instance_lock import BackendInstanceLock

lock = BackendInstanceLock(sys.argv[1])
lock.acquire({"pid": 123, "instance_id": "child-owner"})
Path(sys.argv[2]).write_text("ready", encoding="utf-8")
time.sleep(30)
lock.release()
"""
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(lock_path), str(ready_path)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not ready_path.is_file() and time.monotonic() < deadline:
                    if child.poll() is not None:
                        _stdout, stderr = child.communicate(timeout=1)
                        self.fail(f"lock holder exited before ready: {stderr}")
                    time.sleep(0.01)
                self.assertTrue(
                    ready_path.is_file(), "lock holder did not become ready"
                )

                contender = BackendInstanceLock(lock_path)
                with self.assertRaises(BackendInstanceLockError) as caught:
                    contender.acquire({"instance_id": "parent-contender"})
                self.assertEqual(caught.exception.code, "backend_already_running")
                self.assertEqual(
                    caught.exception.details["owner"]["instance_id"], "child-owner"
                )
            finally:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=3)

            # Stale metadata is informational only; process exit releases the OS
            # lock and the next backend can immediately become owner.
            recovered = BackendInstanceLock(lock_path)
            recovered.acquire({"instance_id": "recovered-owner"})
            recovered.release()


class FakeService:
    def __init__(self, progress, cancel_check):
        self.progress = progress
        self.cancel_check = cancel_check
        self.owner_thread = threading.get_ident()
        self.call_threads: list[int] = []
        self.calls: list[tuple[str, dict]] = []
        self.slow_started = threading.Event()
        self.close_thread: int | None = None

    def _call(self, operation: str, values: dict) -> FakeReport:
        self.call_threads.append(threading.get_ident())
        self.calls.append((operation, values))
        self.cancel_check()
        self.progress("Embedded 2/3 unique images.")
        return FakeReport(operation, values)

    def index_folder(self, folder, **kwargs):
        if folder == "slow":
            self.call_threads.append(threading.get_ident())
            self.calls.append(("index", {"folder": folder, **kwargs}))
            self.slow_started.set()
            for current in range(1, 200):
                self.cancel_check()
                self.progress(f"Embedded {current}/200 unique images.")
                time.sleep(0.005)
            return FakeReport("index", {"folder": folder})
        return self._call("index", {"folder": folder, **kwargs})

    def index_and_auto_tag_folder(self, folder, **kwargs):
        return self._call("index_and_auto_tag", {"folder": folder, **kwargs})

    def sync_folder(self, folder, **kwargs):
        return self._call("sync", {"folder": folder, **kwargs})

    def search_by_text(self, text, **kwargs):
        return self._call("search_text", {"text": text, **kwargs})

    def search_by_tags(self, text, **kwargs):
        return self._call("search_tags", {"text": text, **kwargs})

    def search_by_image(self, image_path, **kwargs):
        return self._call("search_image", {"image": image_path, **kwargs})

    def search_by_image_and_text(self, image_path, text, **kwargs):
        return self._call(
            "search_combined", {"image": image_path, "text": text, **kwargs}
        )

    def stats(self):
        return self._call("stats", {})

    def list_roots(self):
        return self._call("roots", {})

    def clear_embedding_cache(self):
        return self._call("cache_clear", {})

    def clean_results(self, days, **kwargs):
        return self._call("clean_results", {"days": days, **kwargs})

    def estimate_auto_tags(self, **kwargs):
        return self._call("auto_tag_estimate", kwargs)

    def pending_auto_tags(self, **kwargs):
        return self._call("auto_tag_pending", kwargs)

    def auto_tag_images(self, **kwargs):
        return self._call("auto_tag", kwargs)

    def review_auto_tags(self, decisions):
        return self._call("auto_tag_review", {"decisions": decisions})

    def review_auto_tags_batch(self, **kwargs):
        return self._call("auto_tag_review_batch", kwargs)

    def undo_latest_auto_tag_review_batch(self):
        return self._call("auto_tag_review_undo", {})

    def backfill_metadata_embeddings(self, **kwargs):
        return self._call("metadata_backfill", kwargs)

    def close(self):
        self.close_thread = threading.get_ident()


class MultiLibraryFakeService:
    def __init__(self, library, progress, cancel_check, source_path):
        self.library = library
        self.progress = progress
        self.cancel_check = cancel_check
        self.source_path = source_path
        self.owner_thread = threading.get_ident()
        self.call_threads: list[int] = []
        self.prepare_count = 0
        self.query_count = 0
        self.candidate_ks: list[int] = []
        self.block_stats = False
        self.stats_started = threading.Event()
        self.stats_release = threading.Event()
        self.block_query = False
        self.query_started = threading.Event()
        self.query_release = threading.Event()

    def _record(self):
        self.cancel_check()
        self.call_threads.append(threading.get_ident())

    def stats(self):
        self._record()
        self.stats_started.set()
        if self.block_stats:
            self.stats_release.wait(timeout=2)
        return {"tracked_files": 1}

    def search_by_text(self, text, **_kwargs):
        self._record()
        return {
            "query_type": "text",
            "output_dir": "single",
            "result_count": 1,
            "results": [
                {
                    "doc_id": f"{self.library.library_id}-single",
                    "library_id": "",
                    "library_name": "",
                }
            ],
            "library_ids": [],
            "library_names": [],
            "text": text,
        }

    def prepare_search_query(
        self,
        *,
        text=None,
        image_path=None,
        search_mode="semantic",
    ):
        self._record()
        self.prepare_count += 1
        if search_mode == "tags":
            return PreparedSearch(
                query_type="tag",
                search_mode="tags",
                text=text,
            )
        query_type = (
            "image_text"
            if text is not None and image_path is not None
            else "image"
            if image_path is not None
            else "text"
        )
        return PreparedSearch(
            query_type=query_type,
            text=text,
            image_path=image_path,
            text_vector=[1.0] if text is not None else None,
            image_vector=[1.0] if image_path is not None else None,
            embedding_sources={
                **({"image": "api"} if image_path is not None else {}),
                **({"text": "api"} if text is not None else {}),
            },
            request_ids=["one-embedding-request"],
        )

    def query_prepared_search(self, prepared, **kwargs):
        self._record()
        self.query_count += 1
        self.candidate_ks.append(kwargs["candidate_k"])
        self.query_started.set()
        if self.block_query:
            self.query_release.wait(timeout=2)
        shared_hash = "d" * 64 if self.library.library_id == "library-b" else "a" * 64
        if prepared.query_type == "tag":
            hit = SearchHit(
                doc_id=f"{self.library.library_id}-tag-doc",
                distance=0.05 if self.library.library_id == "library-b" else 0.10,
                fields={
                    "root_id": "root",
                    "relative_path": self.source_path.name,
                    "sha256": shared_hash,
                    "tags": ["\u539f\u795e"],
                },
                raw_score=(0.05 if self.library.library_id == "library-b" else 0.10),
                normalized_score=(
                    0.95 if self.library.library_id == "library-b" else 0.90
                ),
                confidence=(0.95 if self.library.library_id == "library-b" else 0.90),
                match_state="high",
                rank_source="tag",
                matched_tags=("\u539f\u795e",),
            )
            return PreparedSearchCandidates(
                query_type="tag",
                hits=[ResolvedSearchHit(hit, str(self.source_path))],
                candidate_k=kwargs["candidate_k"],
                collection_size=1,
                quality_configured=False,
            )
        if prepared.query_type == "image_text":
            image_confidence, text_confidence = (
                (0.82, 0.78) if self.library.library_id == "library-b" else (0.60, 0.70)
            )

            def channel_hit(source: RankSource, confidence: float) -> ResolvedSearchHit:
                distance = 2.0 * (1.0 - confidence)
                hit = SearchHit(
                    doc_id=f"{self.library.library_id}-doc",
                    distance=distance,
                    fields={
                        "root_id": "root",
                        "relative_path": self.source_path.name,
                        "sha256": shared_hash,
                        "tags": [],
                    },
                    rank=1,
                    raw_score=distance,
                    normalized_score=confidence,
                    confidence=confidence,
                    match_state="high" if confidence >= 0.75 else "possible",
                    rank_source=source,
                )
                return ResolvedSearchHit(hit, str(self.source_path))

            return PreparedSearchCandidates(
                query_type="image_text",
                image_hits=[channel_hit("image", image_confidence)],
                text_hits=[channel_hit("text", text_confidence)],
                candidate_k=kwargs["candidate_k"],
                collection_size=1,
                quality_configured=True,
                minimum_confidence=0.25,
                possible_confidence=0.5,
                high_confidence=0.75,
            )
        hit = SearchHit(
            doc_id=f"{self.library.library_id}-doc",
            distance=0.1 if self.library.library_id == "library-b" else 0.2,
            fields={
                "root_id": "root",
                "relative_path": self.source_path.name,
                "sha256": shared_hash,
                "tags": [],
            },
            raw_score=0.9 if self.library.library_id == "library-b" else 0.8,
            normalized_score=0.9 if self.library.library_id == "library-b" else 0.8,
            confidence=0.9 if self.library.library_id == "library-b" else 0.8,
            match_state="high",
            rank_source="text",
        )
        hits = [ResolvedSearchHit(hit, str(self.source_path))]
        if self.library.library_id == "library-a":
            duplicate = SearchHit(
                doc_id="library-a-duplicate",
                distance=0.3,
                fields={
                    "root_id": "root",
                    "relative_path": self.source_path.name,
                    "sha256": "d" * 64,
                    "tags": [],
                },
                raw_score=0.7,
                normalized_score=0.7,
                confidence=0.7,
                match_state="possible",
                rank_source="text",
            )
            hits.append(ResolvedSearchHit(duplicate, str(self.source_path)))
        return PreparedSearchCandidates(
            query_type="text",
            hits=hits,
            candidate_k=kwargs["candidate_k"],
            collection_size=len(hits),
            quality_configured=True,
            minimum_confidence=0.25,
            possible_confidence=0.5,
            high_confidence=0.75,
        )

    def close(self):
        self.call_threads.append(threading.get_ident())


class BackendServerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="zvec_backend_test_"))
        self.query_root = self.temp_dir / "query"
        self.query_root.mkdir()
        self.query_image = self.query_root / "query.png"
        self.query_image.write_bytes(b"fake image")
        self.fake: FakeService | None = None

        def factory(progress, cancel_check):
            self.fake = FakeService(progress, cancel_check)
            return self.fake

        self.token = "test-backend-token"
        self.instance_id = "desktop-test-instance"
        self.config_fingerprint = "f" * 64
        self.instance_lock_path = self.temp_dir / "backend.lock"
        self.server, self.manager = create_backend_server(
            host="127.0.0.1",
            port=0,
            token=self.token,
            instance_id=self.instance_id,
            config_fingerprint=self.config_fingerprint,
            instance_lock_path=self.instance_lock_path,
            query_root=self.query_root,
            service_factory=factory,
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.server_thread.start()
        deadline = time.monotonic() + 2
        while self.fake is None and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertIsNotNone(self.fake)

    def tearDown(self):
        self.server.shutdown()
        self.server_thread.join(timeout=2)
        self.server.server_close()
        self.manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        token: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if body is not None:
            headers["Content-Type"] = content_type
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        connection.close()
        return response.status, json.loads(response_body)

    def submit(self, command: str, params: dict | None = None) -> dict:
        status, payload = self.request(
            "POST", "/v1/jobs", {"command": command, "params": params or {}}
        )
        self.assertEqual(status, 202, payload)
        return payload["job"]

    def wait_for_job(self, job_id: str, timeout: float = 3) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, payload = self.request("GET", f"/v1/jobs/{job_id}")
            self.assertEqual(status, 200, payload)
            job = payload["job"]
            if job["status"] in {
                "succeeded",
                "partial",
                "needs_attention",
                "failed",
                "cancelled",
            }:
                return job
            time.sleep(0.01)
        self.fail(f"job {job_id} did not finish")

    def test_health_and_bearer_authentication(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        connection.request("GET", "/health")
        response = connection.getresponse()
        unauthorized = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 401)
        self.assertEqual(unauthorized["error"]["code"], "unauthorized")

        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")
        self.assertTrue(health["service_ready"])
        self.assertTrue(health["worker_alive"])
        self.assertEqual(health["protocol_version"], 2)
        self.assertEqual(health["instance_id"], self.instance_id)
        self.assertEqual(health["config_fingerprint"], self.config_fingerprint)
        self.assertTrue(health["capabilities"]["multi_library"])
        self.assertTrue(health["capabilities"]["persistent_backend_session"])
        self.assertTrue(health["capabilities"]["graceful_shutdown"])
        self.assertTrue(health["capabilities"]["source_only_search_results"])
        self.assertTrue(health["capabilities"]["large_library_policy_diagnostics"])
        policy = health["large_library_policy"]
        self.assertEqual(policy["schema_version"], 1)
        self.assertEqual(policy["queue"]["capacity_per_library"], 64)
        self.assertEqual(policy["queue"]["cooperative_checkpoint_items"], 200)
        self.assertEqual(policy["collection_writes"]["batch_size"], 256)
        self.assertEqual(policy["results"]["preview_page_size"], 15)
        self.assertEqual(
            policy["large_clustering"]["default_types"],
            ["exact", "perceptual"],
        )
        self.assertRegex(
            health["large_library_policy_sha256"],
            r"^[0-9a-f]{64}$",
        )

        status, version = self.request("GET", "/version")
        self.assertEqual(status, 200)
        self.assertEqual(version["app"], "zvec-image-search")
        self.assertEqual(version["instance_id"], self.instance_id)
        self.assertEqual(version["config_fingerprint"], self.config_fingerprint)
        self.assertTrue(version["capabilities"]["federated_search"])
        self.assertTrue(version["capabilities"]["tag_only_search"])
        self.assertTrue(version["capabilities"]["low_confidence_override"])
        self.assertTrue(version["capabilities"]["hybrid_tag_vector_search"])
        self.assertTrue(version["capabilities"]["result_diversity"])
        self.assertTrue(version["capabilities"]["source_only_search_results"])
        self.assertEqual(
            version["capabilities"]["search_sort_modes"],
            ["confidence", "relevance", "diverse", "legacy"],
        )
        self.assertTrue(version["capabilities"]["index_and_auto_tag"])
        self.assertTrue(version["capabilities"]["auto_tag_review_batch"])
        self.assertTrue(version["capabilities"]["auto_tag_review_undo"])
        self.assertTrue(version["capabilities"]["metadata_embedding_backfill"])
        self.assertEqual(version["large_library_policy"], policy)
        self.assertEqual(
            version["large_library_policy_sha256"],
            health["large_library_policy_sha256"],
        )

    def test_session_credentials_are_authenticated_and_never_echoed(self):
        secret = "dashscope-secret-not-for-output"
        status, payload = self.request(
            "PUT",
            "/v1/session/credentials",
            {"dashscope_api_key": secret, "api_url": "http://127.0.0.1:9999/embed"},
            token="wrong-token",
        )
        self.assertEqual(status, 401)
        self.assertNotIn(secret, json.dumps(payload))

        status, payload = self.request(
            "PUT",
            "/v1/session/credentials",
            {"dashscope_api_key": secret, "api_url": "http://127.0.0.1:9999/embed"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"credentials_configured": True})
        self.assertNotIn(secret, json.dumps(payload))

        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["credentials_configured"])
        self.assertNotIn(secret, json.dumps(health))

    def test_json_endpoints_require_content_length(self):
        for method, path in (
            ("PUT", "/v1/session/credentials"),
            ("POST", "/v1/jobs"),
            ("POST", "/v1/control/shutdown"),
        ):
            with self.subTest(method=method, path=path):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", self.server.server_address[1], timeout=3
                )
                try:
                    # Build the request manually so http.client cannot add the
                    # Content-Length header on our behalf.
                    connection.putrequest(method, path)
                    connection.putheader("Authorization", f"Bearer {self.token}")
                    connection.putheader("Content-Type", "application/json")
                    connection.endheaders()
                    response = connection.getresponse()
                    payload = json.loads(response.read())
                finally:
                    connection.close()

                self.assertEqual(response.status, 411)
                self.assertEqual(payload["error"]["code"], "length_required")
                self.assertEqual(
                    payload["error"]["message"], "Content-Length is required."
                )

    def test_shutdown_rejects_active_jobs_and_lists_their_ids(self):
        running = self.submit("index", {"folder": "slow"})
        assert self.fake is not None
        self.assertTrue(self.fake.slow_started.wait(timeout=1))

        status, payload = self.request(
            "POST", "/v1/control/shutdown", {"if_idle": True}
        )
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["error"]["code"], "backend_busy")
        self.assertIn(running["id"], payload["error"]["details"]["active_job_ids"])
        self.assertTrue(self.server_thread.is_alive())

        self.request("DELETE", f"/v1/jobs/{running['id']}")
        self.assertEqual(self.wait_for_job(running["id"])["status"], "cancelled")

    def test_shutdown_requires_an_explicit_idle_only_request(self):
        for payload in ({}, {"if_idle": False}, {"if_idle": True, "force": True}):
            with self.subTest(payload=payload):
                status, response = self.request("POST", "/v1/control/shutdown", payload)
                self.assertEqual(status, 400, response)
                self.assertIn(
                    response["error"]["code"], {"invalid_request", "unknown_fields"}
                )
                self.assertTrue(self.server_thread.is_alive())

    def test_idle_shutdown_returns_accepted_and_exits_serve_loop(self):
        port = self.server.server_address[1]
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        body = json.dumps({"if_idle": True}).encode("utf-8")
        connection.request(
            "POST",
            "/v1/control/shutdown",
            body=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        transport = connection.sock
        response = connection.getresponse()
        payload = json.loads(response.read())

        self.assertEqual(response.status, 202, payload)
        self.assertEqual(response.getheader("Connection"), "close")
        self.assertEqual(
            payload,
            {
                "accepted": True,
                "status": "shutting_down",
                "instance_id": self.instance_id,
            },
        )
        self.server_thread.join(timeout=2)
        self.assertFalse(self.server_thread.is_alive())
        self.server.server_close()

        # The original keep-alive transport cannot process another /health.
        assert transport is not None
        with self.assertRaises(OSError):
            transport.sendall(
                b"GET /health HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {self.token}\r\n\r\n".encode()
            )
        try:
            # The same HTTPConnection object also cannot silently reconnect after
            # server_close; this mirrors the desktop's long-lived HttpClient.
            with self.assertRaises(OSError):
                connection.request(
                    "GET",
                    "/health",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
        finally:
            connection.close()

        assert self.fake is not None
        self.assertIsNotNone(self.fake.close_thread)

    def test_generated_identity_is_stable_within_one_server(self):
        created: list[FakeService] = []
        config_home = self.temp_dir / "generated-config-home"

        def factory(progress, cancel_check):
            service = FakeService(progress, cancel_check)
            created.append(service)
            return service

        with patch.dict(
            os.environ,
            {
                "ZVEC_CONFIG_HOME": str(config_home),
                "ZVEC_BACKEND_LOCK_PATH": "",
            },
        ):
            server, manager = create_backend_server(
                host="127.0.0.1",
                port=0,
                token="generated-token",
                query_root=self.query_root,
                service_factory=factory,
            )
            try:
                first = manager.version_info()
                second = manager.version_info()
                self.assertRegex(first["instance_id"], r"^[0-9a-f]{32}$")
                self.assertRegex(first["config_fingerprint"], r"^[0-9a-f]{64}$")
                self.assertEqual(first["instance_id"], second["instance_id"])
                self.assertEqual(
                    first["config_fingerprint"], second["config_fingerprint"]
                )
                self.assertTrue((config_home / "backend" / "backend.lock").is_file())
            finally:
                manager.close()
                server.server_close()

    def test_instance_lock_fails_before_a_second_service_is_created(self):
        second_factory_called = threading.Event()

        def second_factory(progress, cancel_check):
            second_factory_called.set()
            return FakeService(progress, cancel_check)

        with self.assertRaises(BackendInstanceLockError) as caught:
            create_backend_server(
                host="127.0.0.1",
                port=0,
                token="second-token",
                instance_lock_path=self.instance_lock_path,
                query_root=self.query_root,
                service_factory=second_factory,
            )
        self.assertEqual(caught.exception.code, "backend_already_running")
        self.assertFalse(second_factory_called.is_set())
        self.assertEqual(
            caught.exception.details["owner"]["instance_id"], self.instance_id
        )

    def test_all_commands_use_one_long_lived_service_worker(self):
        commands: list[tuple[str, dict[str, Any], str]] = [
            ("index", {"folder": "library", "tags": ["featured"]}, "index"),
            (
                "index_and_auto_tag",
                {
                    "folder": "library",
                    "tags": ["featured"],
                    "model": "flash",
                    "max_images": 25,
                    "max_budget_cny": 2.5,
                    "external_processing_confirmed": True,
                },
                "index_and_auto_tag",
            ),
            ("folder_tag_backfill", {"folder": "library"}, "index"),
            (
                "auto_tag_estimate",
                {"scope": "untagged", "max_images": 25},
                "auto_tag_estimate",
            ),
            ("auto_tag_pending", {}, "auto_tag_pending"),
            (
                "auto_tag",
                {
                    "scope": "latest_index_run",
                    "model": "flash",
                    "max_images": 10,
                    "max_budget_cny": 1.0,
                    "external_processing_confirmed": True,
                },
                "auto_tag",
            ),
            (
                "auto_tag_review",
                {"decisions": [{"doc_id": "doc-1", "action": "accept"}]},
                "auto_tag_review",
            ),
            (
                "auto_tag_review_batch",
                {
                    "proposal_ids": ["doc-1"],
                    "accepted_tags_by_proposal": {"doc-1": ["Cosplay"]},
                    "exclude_identity_tags": True,
                },
                "auto_tag_review_batch",
            ),
            ("auto_tag_review_undo", {}, "auto_tag_review_undo"),
            ("metadata_backfill", {"max_images": 25}, "metadata_backfill"),
            ("sync", {"folder": "library", "dry_run": True}, "sync"),
            ("search", {"text": "red", "top_k": 4}, "search_text"),
            (
                "search",
                {"text": "\u539f", "search_mode": "tags"},
                "search_tags",
            ),
            (
                "search",
                {"image": str(self.query_image), "include_self": True},
                "search_image",
            ),
            (
                "search",
                {
                    "text": "red",
                    "image": str(self.query_image),
                    "image_weight": 0.7,
                    "text_weight": 0.3,
                },
                "search_combined",
            ),
            ("stats", {}, "stats"),
            ("roots", {}, "roots"),
            ("cache_clear", {}, "cache_clear"),
            ("clean_results", {"days": 3, "dry_run": True}, "clean_results"),
        ]
        for command, params, expected_operation in commands:
            job = self.submit(command, params)
            completed = self.wait_for_job(job["id"])
            self.assertEqual(completed["status"], "succeeded", completed)
            self.assertEqual(completed["result"]["operation"], expected_operation)
            self.assertIsNone(completed["error"])

        assert self.fake is not None
        self.assertEqual(set(self.fake.call_threads), {self.fake.owner_thread})

    def test_single_library_search_uses_source_only_bounded_publication(self):
        assert self.fake is not None
        cases = (
            ({"text": "red"}, "search_text"),
            ({"text": "原", "search_mode": "tags"}, "search_tags"),
            ({"image": str(self.query_image)}, "search_image"),
            (
                {"text": "red", "image": str(self.query_image)},
                "search_combined",
            ),
        )

        for params, expected_operation in cases:
            with self.subTest(operation=expected_operation):
                completed = self.wait_for_job(self.submit("search", params)["id"])
                self.assertEqual(completed["status"], "succeeded", completed)
                operation, values = self.fake.calls[-1]
                self.assertEqual(operation, expected_operation)
                self.assertIs(values["copy_files"], False)
                self.assertEqual(values["report_result_limit"], 15)

    def test_index_and_auto_tag_normalizes_and_preserves_combined_result(self):
        assert self.fake is not None

        def combined(folder, **kwargs):
            self.fake.call_threads.append(threading.get_ident())
            self.fake.calls.append(("index_and_auto_tag", {"folder": folder, **kwargs}))
            return {
                "failed": 2,
                "needs_attention": False,
                "index": {"inserted": 8, "failed": 1},
                "auto_tag": {"proposed": 7, "failed": 1},
                "pipeline": {
                    "overlap_observed": True,
                    "embedding_peak_in_flight": 2,
                    "flash_peak_in_flight": 2,
                },
            }

        self.fake.index_and_auto_tag_folder = combined  # type: ignore[method-assign]
        completed = self.wait_for_job(
            self.submit(
                "index_and_auto_tag",
                {
                    "folder": "library",
                    "recursive": False,
                    "verify_hash": True,
                    "tags": ["new", "featured"],
                    "model": "qwen3-vl-flash",
                    "max_images": 321,
                    "max_budget_cny": 3.5,
                    "external_processing_confirmed": True,
                },
            )["id"]
        )
        self.assertEqual(completed["status"], "partial")
        self.assertEqual(completed["failure_count"], 2)
        self.assertEqual(completed["result"]["failed"], 2)
        self.assertEqual(completed["result"]["index"], {"inserted": 8, "failed": 1})
        self.assertEqual(completed["result"]["auto_tag"], {"proposed": 7, "failed": 1})
        self.assertTrue(completed["result"]["pipeline"]["overlap_observed"])
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "index_and_auto_tag")
        self.assertEqual(
            call,
            {
                "folder": "library",
                "recursive": False,
                "verify_hash": True,
                "tags": ["new", "featured"],
                "model": "qwen3-vl-flash",
                "max_images": 321,
                "max_budget_cny": 3.5,
                "external_processing_confirmed": True,
            },
        )

    def test_index_and_auto_tag_defaults_and_rejects_invalid_params(self):
        completed = self.wait_for_job(
            self.submit("index_and_auto_tag", {"folder": "library"})["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "index_and_auto_tag")
        self.assertEqual(
            call,
            {
                "folder": "library",
                "recursive": True,
                "verify_hash": False,
                "tags": None,
                "model": "qwen3-vl-flash",
                "max_images": 200,
                "max_budget_cny": None,
                "external_processing_confirmed": False,
            },
        )

        invalid_cases = (
            {"folder": "library", "unknown": True},
            {"library_id": "", "folder": "library"},
            {"folder": 123},
            {"folder": "library", "recursive": "yes"},
            {"folder": "library", "verify_hash": 1},
            {"folder": "library", "tags": "featured"},
            {"folder": "library", "model": "unsupported"},
            {"folder": "library", "max_images": 0},
            {"folder": "library", "max_images": 10_001},
            {"folder": "library", "max_budget_cny": 0},
            {"folder": "library", "external_processing_confirmed": "yes"},
        )
        for params in invalid_cases:
            with self.subTest(params=params):
                status, payload = self.request(
                    "POST",
                    "/v1/jobs",
                    {"command": "index_and_auto_tag", "params": params},
                )
                self.assertEqual(status, 400, payload)
                self.assertIn(
                    payload["error"]["code"], {"invalid_params", "unknown_params"}
                )

    def test_index_preserves_tag_intent_for_new_tags_clear_and_no_change(self):
        cases: tuple[tuple[dict[str, object], list[str] | None], ...] = (
            (
                {"folder": "library", "tags": ["new", "featured"]},
                ["new", "featured"],
            ),
            ({"folder": "library", "tags": []}, []),
            ({"folder": "library"}, None),
        )
        for params, expected_tags in cases:
            job = self.submit("index", params)
            completed = self.wait_for_job(job["id"])
            self.assertEqual(completed["status"], "succeeded", completed)
            assert self.fake is not None
            operation, call = self.fake.calls[-1]
            self.assertEqual(operation, "index")
            self.assertEqual(call["tags"], expected_tags)

    def test_execution_tuning_is_not_a_per_job_backend_contract(self):
        for command in ("index", "sync", "index_and_auto_tag", "auto_tag"):
            for field, value in (("concurrency", 4), ("skip_errors", True)):
                with self.subTest(command=command, field=field):
                    status, payload = self.request(
                        "POST",
                        "/v1/jobs",
                        {"command": command, "params": {field: value}},
                    )
                    self.assertEqual(status, 400, payload)
                    self.assertEqual(payload["error"]["code"], "unknown_params")

    def test_folder_tag_backfill_never_injects_manual_tags(self):
        job = self.submit(
            "folder_tag_backfill",
            {"folder": "library", "recursive": False, "verify_hash": True},
        )
        completed = self.wait_for_job(job["id"])
        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "index")
        self.assertIsNone(call["tags"])
        self.assertFalse(call["recursive"])
        self.assertTrue(call["verify_hash"])

    def test_metadata_backfill_is_explicit_and_validates_request_limit(self):
        completed = self.wait_for_job(
            self.submit("metadata_backfill", {"max_images": 321})["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "metadata_backfill")
        self.assertEqual(call, {"max_images": 321})

        for params in (
            {"max_images": 0},
            {"max_images": 10_001},
            {"max_images": True},
            {"unknown": 1},
        ):
            with self.subTest(params=params):
                status, payload = self.request(
                    "POST",
                    "/v1/jobs",
                    {"command": "metadata_backfill", "params": params},
                )
                self.assertEqual(status, 400, payload)
                self.assertIn(
                    payload["error"]["code"],
                    {"invalid_params", "unknown_params"},
                )

    def test_auto_tag_review_accepts_desktop_decision_contract(self):
        completed = self.wait_for_job(
            self.submit(
                "auto_tag_review",
                {
                    "decisions": [
                        {
                            "proposal_id": "doc-1",
                            "decision": "accept",
                            "accepted_tags": ["原神", "刻晴"],
                        }
                    ]
                },
            )["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "auto_tag_review")
        self.assertEqual(
            call["decisions"],
            [
                {
                    "doc_id": "doc-1",
                    "action": "accept",
                    "tags": ["原神", "刻晴"],
                }
            ],
        )

    def test_auto_tag_pending_defaults_and_validates_pagination(self):
        completed = self.wait_for_job(self.submit("auto_tag_pending")["id"])
        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "auto_tag_pending")
        self.assertEqual(call, {"offset": 0, "limit": 100})

        completed = self.wait_for_job(
            self.submit("auto_tag_pending", {"offset": 125, "limit": 500})["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "auto_tag_pending")
        self.assertEqual(call, {"offset": 125, "limit": 500})

        for params in (
            {"offset": -1},
            {"offset": True},
            {"limit": 0},
            {"limit": 501},
            {"limit": "100"},
        ):
            status, payload = self.request(
                "POST",
                "/v1/jobs",
                {"command": "auto_tag_pending", "params": params},
            )
            self.assertEqual(status, 400, payload)
            self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_auto_tag_pending_normalizes_review_workbench_filters(self):
        completed = self.wait_for_job(
            self.submit(
                "auto_tag_pending",
                {
                    "offset": 25,
                    "limit": 100,
                    "filters": {
                        "latest_index_only": True,
                        "character": "刻晴",
                        "work": "原神",
                        "action": "行走",
                        "expression": "微笑",
                        "review_state": "identity",
                    },
                },
            )["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "auto_tag_pending")
        self.assertEqual(call["offset"], 25)
        self.assertEqual(call["limit"], 100)
        self.assertEqual(
            call["filters"],
            {
                "latest_index_only": True,
                "character": "刻晴",
                "work": "原神",
                "action": "行走",
                "expression": "微笑",
                "review_state": "identity",
            },
        )
        for filters in (
            {"review_state": "accepted"},
            {"latest_index_only": "yes"},
            {"unknown": True},
            {"action": "x" * 129},
        ):
            status, payload = self.request(
                "POST",
                "/v1/jobs",
                {
                    "command": "auto_tag_pending",
                    "params": {"filters": filters},
                },
            )
            self.assertEqual(status, 400, payload)
            self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_batch_review_and_undo_validate_desktop_contract(self):
        completed = self.wait_for_job(
            self.submit(
                "auto_tag_review_batch",
                {
                    "proposal_ids": ["doc-1", "doc-2"],
                    "accepted_tags_by_proposal": {
                        "doc-1": ["Cosplay"],
                        "doc-2": ["全身"],
                    },
                    "exclude_identity_tags": True,
                },
            )["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "auto_tag_review_batch")
        self.assertEqual(call["proposal_ids"], ["doc-1", "doc-2"])
        self.assertEqual(
            call["accepted_tags_by_proposal"],
            {"doc-1": ["Cosplay"], "doc-2": ["全身"]},
        )
        self.assertTrue(call["exclude_identity_tags"])
        self.assertEqual(call["acceptance_mode"], "low_risk_only")
        self.assertFalse(call["batch_confirmation"])

        undone = self.wait_for_job(self.submit("auto_tag_review_undo")["id"])
        self.assertEqual(undone["status"], "succeeded", undone)
        self.assertEqual(self.fake.calls[-1][0], "auto_tag_review_undo")

        invalid_cases = (
            {"proposal_ids": []},
            {"proposal_ids": ["doc-1"], "exclude_identity_tags": False},
            {
                "proposal_ids": ["doc-1"],
                "accepted_tags_by_proposal": {"doc-2": ["Cosplay"]},
            },
            {
                "proposal_ids": ["doc-1"],
                "accepted_tags_by_proposal": {"doc-1": "Cosplay"},
            },
        )
        for params in invalid_cases:
            status, payload = self.request(
                "POST",
                "/v1/jobs",
                {"command": "auto_tag_review_batch", "params": params},
            )
            self.assertEqual(status, 400, payload)
            self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_batch_review_forwards_explicit_recommended_identity_mode(self):
        completed = self.wait_for_job(
            self.submit(
                "auto_tag_review_batch",
                {
                    "proposal_ids": ["doc-1"],
                    "accepted_tags_by_proposal": {
                        "doc-1": ["Cosplay", "刻晴"],
                    },
                    "acceptance_mode": "recommended",
                    "batch_confirmation": True,
                    "exclude_identity_tags": False,
                },
            )["id"]
        )

        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        operation, call = self.fake.calls[-1]
        self.assertEqual(operation, "auto_tag_review_batch")
        self.assertEqual(call["acceptance_mode"], "recommended")
        self.assertTrue(call["batch_confirmation"])
        self.assertFalse(call["exclude_identity_tags"])

    def test_single_review_forwards_confirmed_identity_tags(self):
        completed = self.wait_for_job(
            self.submit(
                "auto_tag_review",
                {
                    "decisions": [
                        {
                            "proposal_id": "doc-1",
                            "decision": "accept",
                            "accepted_tags": ["刻晴"],
                            "confirmed_identity_tags": ["刻晴"],
                        }
                    ]
                },
            )["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        self.assertEqual(
            self.fake.calls[-1][1]["decisions"][0]["confirmed_identity_tags"],
            ["刻晴"],
        )

    def test_query_images_are_limited_to_one_direct_child_file(self):
        outside = self.temp_dir / "outside.png"
        outside.write_bytes(b"outside")
        nested_dir = self.query_root / "nested"
        nested_dir.mkdir()
        nested = nested_dir / "nested.png"
        nested.write_bytes(b"nested")

        for path in (outside, nested):
            status, payload = self.request(
                "POST",
                "/v1/jobs",
                {"command": "search", "params": {"image": str(path)}},
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "invalid_query_image")

        accepted = self.submit("search", {"image": str(self.query_image)})
        self.assertEqual(self.wait_for_job(accepted["id"])["status"], "succeeded")

    def test_low_confidence_override_defaults_off_and_requires_a_boolean(self):
        default_job = self.wait_for_job(self.submit("search", {"text": "red"})["id"])
        self.assertEqual(default_job["status"], "succeeded", default_job)
        assert self.fake is not None
        self.assertFalse(self.fake.calls[-1][1]["show_low_confidence"])

        override_job = self.wait_for_job(
            self.submit("search", {"text": "red", "show_low_confidence": True})["id"]
        )
        self.assertEqual(override_job["status"], "succeeded", override_job)
        self.assertTrue(self.fake.calls[-1][1]["show_low_confidence"])

        status, payload = self.request(
            "POST",
            "/v1/jobs",
            {
                "command": "search",
                "params": {"text": "red", "show_low_confidence": "yes"},
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_search_sort_mode_defaults_to_confidence_and_rejects_unknown_values(self):
        default_job = self.wait_for_job(self.submit("search", {"text": "red"})["id"])
        self.assertEqual(default_job["status"], "succeeded", default_job)
        assert self.fake is not None
        self.assertEqual(self.fake.calls[-1][1]["sort_mode"], "confidence")

        for sort_mode in ("relevance", "diverse", "legacy"):
            completed = self.wait_for_job(
                self.submit(
                    "search",
                    {"text": "red", "sort_mode": sort_mode},
                )["id"]
            )
            self.assertEqual(completed["status"], "succeeded", completed)
            self.assertEqual(self.fake.calls[-1][1]["sort_mode"], sort_mode)

        status, payload = self.request(
            "POST",
            "/v1/jobs",
            {
                "command": "search",
                "params": {"text": "red", "sort_mode": "normalized_score"},
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_result_diversity_defaults_on_and_requires_a_boolean(self):
        default_job = self.wait_for_job(self.submit("search", {"text": "red"})["id"])
        self.assertEqual(default_job["status"], "succeeded", default_job)
        assert self.fake is not None
        self.assertTrue(self.fake.calls[-1][1]["diversify_results"])

        original_order_job = self.wait_for_job(
            self.submit(
                "search",
                {"text": "red", "diversify_results": False},
            )["id"]
        )
        self.assertEqual(original_order_job["status"], "succeeded", original_order_job)
        self.assertFalse(self.fake.calls[-1][1]["diversify_results"])

        status, payload = self.request(
            "POST",
            "/v1/jobs",
            {
                "command": "search",
                "params": {"text": "red", "diversify_results": "no"},
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_tag_only_search_is_explicit_and_rejects_image_queries(self):
        completed = self.wait_for_job(
            self.submit(
                "search",
                {
                    "text": "\u795e",
                    "search_mode": "tags",
                    "tags": ["\u89d2\u8272"],
                    "tag_mode": "any",
                },
            )["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        assert self.fake is not None
        operation, values = self.fake.calls[-1]
        self.assertEqual(operation, "search_tags")
        self.assertEqual(values["text"], "\u795e")
        self.assertEqual(values["tags"], ["\u89d2\u8272"])
        self.assertEqual(values["tag_mode"], "any")

        invalid_params = (
            {"text": "tag", "search_mode": "invalid"},
            {
                "text": "tag",
                "image": str(self.query_image),
                "search_mode": "tags",
            },
            {"image": str(self.query_image), "search_mode": "tags"},
        )
        for params in invalid_params:
            with self.subTest(params=params):
                status, payload = self.request(
                    "POST",
                    "/v1/jobs",
                    {"command": "search", "params": params},
                )
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["error"]["code"], "invalid_params")

    def test_running_and_queued_jobs_can_be_cancelled(self):
        running = self.submit("index", {"folder": "slow"})
        assert self.fake is not None
        self.assertTrue(self.fake.slow_started.wait(timeout=1))
        queued = self.submit("stats")

        status, queued_payload = self.request("DELETE", f"/v1/jobs/{queued['id']}")
        self.assertEqual(status, 202)
        self.assertEqual(queued_payload["job"]["status"], "cancelled")

        status, running_payload = self.request("DELETE", f"/v1/jobs/{running['id']}")
        self.assertEqual(status, 202)
        self.assertIn(running_payload["job"]["status"], {"cancelling", "cancelled"})
        self.assertEqual(self.wait_for_job(running["id"])["status"], "cancelled")
        self.assertEqual(self.wait_for_job(queued["id"])["status"], "cancelled")

    def test_item_failures_complete_as_partial_without_stopping_backend(self):
        assert self.fake is not None
        self.fake.stats = lambda: FakeReport("stats", {"failed": 2})  # type: ignore[method-assign]
        completed = self.wait_for_job(self.submit("stats")["id"])
        self.assertEqual(completed["status"], "partial")
        self.assertEqual(completed["failure_count"], 2)

        self.fake.stats = lambda: FakeReport("stats", {})  # type: ignore[method-assign]
        following = self.wait_for_job(self.submit("stats")["id"])
        self.assertEqual(following["status"], "succeeded")

    def test_failed_job_does_not_stop_following_jobs_or_backend(self):
        assert self.fake is not None

        def fail_stats():
            raise RuntimeError("injected single-job failure")

        self.fake.stats = fail_stats  # type: ignore[method-assign]
        failed = self.wait_for_job(self.submit("stats")["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "job_failed")

        self.fake.stats = lambda: FakeReport("stats", {})  # type: ignore[method-assign]
        following = self.wait_for_job(self.submit("stats")["id"])
        self.assertEqual(following["status"], "succeeded")
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["service_ready"])

    def test_systemic_result_finishes_as_needs_attention(self):
        assert self.fake is not None
        self.fake.stats = lambda: FakeReport(  # type: ignore[method-assign]
            "stats", {"failed": 1, "needs_attention": True}
        )
        completed = self.wait_for_job(self.submit("stats")["id"])
        self.assertEqual(completed["status"], "needs_attention")
        self.assertEqual(completed["failure_count"], 1)

    def test_validation_and_unknown_jobs_have_stable_error_objects(self):
        status, payload = self.request(
            "POST",
            "/v1/jobs",
            {"command": "stats", "params": {"unexpected": True}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "unknown_params")
        self.assertIn("message", payload["error"])
        self.assertIn("details", payload["error"])

        status, payload = self.request("GET", f"/v1/jobs/{'0' * 32}")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "job_not_found")

    def test_service_is_closed_by_its_owner_thread(self):
        assert self.fake is not None
        owner_thread = self.fake.owner_thread
        self.server.shutdown()
        self.server_thread.join(timeout=2)
        self.server.server_close()
        self.manager.close()
        self.assertEqual(self.fake.close_thread, owner_thread)


class MultiLibraryBackendTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="zvec_multi_backend_test_"))
        self.query_root = self.temp_dir / "query"
        self.query_root.mkdir()
        self.results = self.temp_dir / "results"
        libraries = (
            LibraryDefinition(
                "library-a",
                "Library A",
                self.temp_dir / "images-a",
                self.temp_dir / "workspace-a",
            ),
            LibraryDefinition(
                "library-b",
                "Library B",
                self.temp_dir / "images-b",
                self.temp_dir / "workspace-b",
            ),
            LibraryDefinition(
                "library-disabled",
                "Disabled",
                self.temp_dir / "images-disabled",
                self.temp_dir / "workspace-disabled",
                enabled=False,
            ),
        )
        self.catalog = LibraryCatalog("library-a", libraries, self.results)
        self.services: dict[str, MultiLibraryFakeService] = {}
        for library in libraries:
            if library.enabled:
                assert library.image_root is not None
                library.image_root.mkdir()
        sources = {
            library.library_id: library.image_root / f"source-{library.library_id}.jpg"
            for library in libraries
            if library.enabled and library.image_root is not None
        }
        for source in sources.values():
            source.write_bytes(source.name.encode())

        def factory(library, progress, cancel_check):
            service = MultiLibraryFakeService(
                library,
                progress,
                cancel_check,
                sources[library.library_id],
            )
            self.services[library.library_id] = service
            return service

        self.token = "multi-library-token"
        self.server, self.manager = create_backend_server(
            host="127.0.0.1",
            port=0,
            token=self.token,
            instance_lock_path=self.temp_dir / "backend.lock",
            config=ServiceConfig(
                workspace=self.temp_dir / "control",
                results_directory=self.results,
            ),
            query_root=self.query_root,
            library_catalog=self.catalog,
            library_service_factory=factory,
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.server_thread.start()
        deadline = time.monotonic() + 2
        while len(self.services) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(set(self.services), {"library-a", "library-b"})
        self.assertEqual(
            len({service.owner_thread for service in self.services.values()}), 2
        )

    def tearDown(self):
        self.server.shutdown()
        self.server_thread.join(timeout=2)
        self.server.server_close()
        self.manager.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def request(
        self, method: str, path: str, payload: dict | None = None
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read())
        connection.close()
        return response.status, data

    def submit(self, command: str, params: dict | None = None) -> dict:
        status, payload = self.request(
            "POST", "/v1/jobs", {"command": command, "params": params or {}}
        )
        self.assertEqual(status, 202, payload)
        return payload["job"]

    def wait_for_job(self, job_id: str, timeout: float = 3) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, payload = self.request("GET", f"/v1/jobs/{job_id}")
            self.assertEqual(status, 200, payload)
            if payload["job"]["status"] in {
                "succeeded",
                "partial",
                "needs_attention",
                "failed",
                "cancelled",
            }:
                return payload["job"]
            time.sleep(0.01)
        self.fail(f"job {job_id} did not finish")

    def test_libraries_and_single_library_routing(self):
        libraries = self.wait_for_job(self.submit("libraries")["id"])
        self.assertEqual(libraries["status"], "succeeded")
        self.assertEqual(libraries["result"]["default_library_id"], "library-a")
        self.assertEqual(len(libraries["result"]["libraries"]), 3)

        stats = self.wait_for_job(
            self.submit("stats", {"library_id": "library-b"})["id"]
        )
        self.assertEqual(stats["result"]["library_id"], "library-b")
        self.assertEqual(stats["result"]["library_name"], "Library B")
        self.assertEqual(len(self.services["library-b"].call_threads), 1)
        self.assertEqual(self.services["library-a"].call_threads, [])
        self.assertEqual(
            self.services["library-b"].call_threads[0],
            self.services["library-b"].owner_thread,
        )

    def test_different_libraries_run_in_parallel_while_each_library_stays_serial(self):
        service_a = self.services["library-a"]
        service_b = self.services["library-b"]
        service_a.block_stats = True
        service_b.block_stats = True

        first_a = self.submit("stats", {"library_id": "library-a"})
        first_b = self.submit("stats", {"library_id": "library-b"})
        self.assertTrue(service_a.stats_started.wait(timeout=1))
        self.assertTrue(service_b.stats_started.wait(timeout=1))

        second_a = self.submit("stats", {"library_id": "library-a"})
        time.sleep(0.05)
        self.assertEqual(len(service_a.call_threads), 1)

        service_a.stats_release.set()
        service_b.stats_release.set()
        for job in (first_a, first_b, second_a):
            self.assertEqual(self.wait_for_job(job["id"])["status"], "succeeded")
        self.assertEqual(len(service_a.call_threads), 2)

    def test_job_list_filters_active_jobs_and_validates_query(self):
        service = self.services["library-a"]
        service.block_stats = True
        job = self.submit("stats", {"library_id": "library-a"})
        self.assertTrue(service.stats_started.wait(timeout=1))

        status, payload = self.request("GET", "/v1/jobs?active=true&limit=10")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["jobs"][0]["id"], job["id"])

        status, payload = self.request("GET", "/v1/jobs?active=maybe")
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["error"]["code"], "invalid_params")

        service.stats_release.set()
        self.wait_for_job(job["id"])
        status, payload = self.request("GET", "/v1/jobs?active=false")
        self.assertEqual(status, 200, payload)
        self.assertIn(job["id"], {item["id"] for item in payload["jobs"]})

    def test_cross_library_search_embeds_once_and_deduplicates_globally(self):
        completed = self.wait_for_job(self.submit("search", {"text": "red"})["id"])
        self.assertEqual(completed["status"], "succeeded", completed)
        result = completed["result"]
        self.assertEqual(result["library_ids"], ["library-a", "library-b"])
        self.assertEqual(result["request_ids"], ["one-embedding-request"])
        self.assertEqual(
            sum(service.prepare_count for service in self.services.values()), 1
        )
        self.assertEqual(
            {
                library_id: service.query_count
                for library_id, service in self.services.items()
            },
            {"library-a": 1, "library-b": 1},
        )
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["filtered_count"], 0)
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertEqual(
            [hit["library_id"] for hit in result["results"]],
            ["library-b", "library-a"],
        )
        self.assertEqual(
            [hit["sha256"] for hit in result["results"]],
            ["d" * 64, "a" * 64],
        )
        for service in self.services.values():
            self.assertEqual(set(service.call_threads), {service.owner_thread})
        manifest = json.loads(
            (Path(result["output_dir"]) / "results.json").read_text("utf-8")
        )
        self.assertEqual(manifest["results"][0]["library_name"], "Library B")
        self.assertEqual(manifest["results"][0]["sha256"], "d" * 64)
        self.assertEqual(manifest["status"], "ok")
        self.assertEqual(manifest["candidate_count"], 2)
        self.assertEqual(manifest["filtered_count"], 0)
        self.assertEqual(manifest["ranking_mode"], "confidence")
        self.assertEqual(manifest["search_quality"]["ranking_mode"], "confidence")
        self.assertEqual(result["result_storage"], "source_only")
        self.assertLessEqual(len(result["results"]), 15)
        self.assertTrue(all(hit["copied_file"] is None for hit in result["results"]))
        output_dir = Path(result["output_dir"])
        self.assertEqual(
            {path.name for path in output_dir.iterdir()},
            {RESULT_OWNERSHIP_MARKER, "results.json", RESULT_STORE_FILENAME},
        )

    def test_federated_query_starts_all_collections_before_waiting_for_results(self):
        service_a = self.services["library-a"]
        service_b = self.services["library-b"]
        for service in (service_a, service_b):
            service.block_query = True
            self.addCleanup(service.query_release.set)

        job = self.submit("search", {"text": "red"})

        # If the coordinator queried Collections serially, library B could not
        # enter its owner thread while library A is deliberately blocked here.
        self.assertTrue(service_a.query_started.wait(timeout=1))
        self.assertTrue(service_b.query_started.wait(timeout=1))
        service_a.query_release.set()
        service_b.query_release.set()
        self.assertEqual(self.wait_for_job(job["id"])["status"], "succeeded")

    def test_cross_library_tag_search_uses_no_embedding_request(self):
        completed = self.wait_for_job(
            self.submit(
                "search",
                {"text": "\u795e", "search_mode": "tags", "top_k": 2},
            )["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        result = completed["result"]
        self.assertEqual(result["request_ids"], [])
        self.assertEqual(result["embedding_sources"], {})
        self.assertEqual(result["ranking_mode"], "tag_match")
        self.assertEqual(result["query_type"], "tag")
        self.assertTrue(all(hit["rank_source"] == "tag" for hit in result["results"]))
        self.assertTrue(
            all(hit["matched_tags"] == ["\u539f\u795e"] for hit in result["results"])
        )
        self.assertEqual(
            sum(service.prepare_count for service in self.services.values()),
            1,
        )

    def test_cross_library_combined_api_uses_fused_raw_score_contract(self):
        query_image = self.query_root / "combined.png"
        query_image.write_bytes(b"fake combined query")
        completed = self.wait_for_job(
            self.submit(
                "search",
                {"text": "red", "image": str(query_image), "top_k": 2},
            )["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        result = completed["result"]
        self.assertEqual(result["ranking_mode"], "confidence")
        self.assertEqual(result["result_count"], 2)
        self.assertTrue(all(hit["rank_source"] == "fused" for hit in result["results"]))
        self.assertTrue(
            all(hit["raw_score"] == hit["confidence"] for hit in result["results"])
        )
        manifest = json.loads(
            (Path(result["output_dir"]) / "results.json").read_text("utf-8")
        )
        self.assertTrue(
            all(hit["raw_score"] == hit["confidence"] for hit in manifest["results"])
        )

    def test_federated_candidate_budget_is_dynamic_and_configurable(self):
        completed = self.wait_for_job(
            self.submit("search", {"text": "red", "candidate_k": 7})["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        for service in self.services.values():
            self.assertEqual(service.candidate_ks, [10])

        completed = self.wait_for_job(self.submit("search", {"text": "red"})["id"])
        self.assertEqual(completed["status"], "succeeded", completed)
        for service in self.services.values():
            self.assertEqual(service.candidate_ks, [10, 50])

        completed = self.wait_for_job(
            self.submit("search", {"text": "red", "candidate_k": 101})["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        for service in self.services.values():
            self.assertEqual(service.candidate_ks[-1], 101)

        completed = self.wait_for_job(
            self.submit("search", {"text": "red", "top_k": 75})["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        for service in self.services.values():
            self.assertEqual(service.candidate_ks[-1], 112)
        result = completed["result"]
        self.assertEqual(result["sort_mode"], "confidence")
        self.assertFalse(result["ranking_diagnostics"]["normalized_score_used"])
        manifest = json.loads(
            (Path(result["output_dir"]) / "results.json").read_text("utf-8")
        )
        self.assertEqual(manifest["sort_mode"], "confidence")
        self.assertEqual(
            manifest["ranking_diagnostics"]["primary"],
            "ranking_confidence_or_confidence_desc",
        )

    def test_federated_low_confidence_override_is_recorded_in_result_and_manifest(self):
        completed = self.wait_for_job(
            self.submit("search", {"text": "red", "show_low_confidence": True})["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        result = completed["result"]
        self.assertEqual(result["status"], "low_confidence_override")
        self.assertTrue(result["show_low_confidence"])
        self.assertTrue(result["low_confidence_override"])
        self.assertTrue(result["search_quality"]["low_confidence_override"])
        self.assertTrue(result["search_quality"]["filtering_overridden"])
        self.assertEqual(result["result_count"], 2)
        manifest = json.loads(
            (Path(result["output_dir"]) / "results.json").read_text("utf-8")
        )
        self.assertTrue(manifest["show_low_confidence"])
        self.assertTrue(manifest["low_confidence_override"])
        self.assertEqual(manifest["status"], "low_confidence_override")
        self.assertTrue(manifest["search_quality"]["low_confidence_override"])

    def test_single_search_keeps_existing_service_api_and_adds_attribution(self):
        completed = self.wait_for_job(
            self.submit("search", {"text": "red", "library_ids": ["library-b"]})["id"]
        )
        self.assertEqual(completed["status"], "succeeded", completed)
        self.assertEqual(completed["result"]["library_id"], "library-b")
        self.assertEqual(completed["result"]["results"][0]["library_name"], "Library B")
        self.assertEqual(self.services["library-b"].prepare_count, 0)

    def test_single_search_job_json_keeps_only_a_first_page_preview(self):
        service = self.services["library-b"]
        preview = [
            ExportedHit(
                rank=index + 1,
                distance=0.1,
                root_id="root",
                relative_path=f"preview-{index}.jpg",
                copied_file=f"preview-{index}.jpg",
                doc_id=f"preview-{index}",
            )
            for index in range(15)
        ]
        omitted = ExportedHit(
            rank=16,
            distance=0.2,
            root_id="root",
            relative_path="must-not-enter-job-json.jpg",
            copied_file="must-not-enter-job-json.jpg",
            doc_id="must-not-enter-job-json",
        )

        def huge_search(text, **_kwargs):
            service._record()
            del text
            return SearchReport(
                query_type="text",
                output_dir="single",
                result_count=100_000,
                results=preview + [omitted] * (100_000 - len(preview)),
            )

        service.search_by_text = huge_search
        completed = self.wait_for_job(
            self.submit("search", {"text": "red", "library_ids": ["library-b"]})["id"]
        )

        result = completed["result"]
        encoded = json.dumps(completed, ensure_ascii=False)
        self.assertEqual(result["result_count"], 100_000)
        self.assertEqual(len(result["results"]), 15)
        self.assertEqual(result["results_inline_count"], 15)
        self.assertTrue(result["results_truncated"])
        self.assertNotIn("must-not-enter-job-json", encoded)
        self.assertLess(len(encoded), 16 * 1024)

    def test_federated_search_job_json_keeps_only_a_first_page_preview(self):
        preview = [{"doc_id": f"preview-{index}"} for index in range(15)]
        omitted = {"doc_id": "must-not-enter-federated-job-json"}
        huge_result = {
            "query_type": "text",
            "output_dir": "federated",
            "result_count": 100_000,
            "results": preview + [omitted] * (100_000 - len(preview)),
            "candidate_count": 100_000,
        }

        with patch(
            "image_vector_service.backend_server.export_federated_search",
            return_value=huge_result,
        ) as export_search:
            completed = self.wait_for_job(self.submit("search", {"text": "red"})["id"])

        result = completed["result"]
        encoded = json.dumps(completed, ensure_ascii=False)
        self.assertEqual(result["result_count"], 100_000)
        self.assertEqual(len(result["results"]), 15)
        self.assertEqual(result["results_inline_count"], 15)
        self.assertTrue(result["results_truncated"])
        self.assertNotIn("must-not-enter-federated-job-json", encoded)
        self.assertLess(len(encoded), 16 * 1024)
        self.assertFalse(export_search.call_args.kwargs["copy_files"])
        self.assertEqual(export_search.call_args.kwargs["report_result_limit"], 15)

    def test_unknown_and_disabled_library_selection_is_rejected(self):
        for library_id, code in (
            ("missing", "library_not_found"),
            ("library-disabled", "library_disabled"),
        ):
            status, payload = self.request(
                "POST",
                "/v1/jobs",
                {
                    "command": "stats",
                    "params": {"library_id": library_id},
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], code)


class ServeCommandTest(unittest.TestCase):
    def test_lock_failure_is_emitted_as_structured_startup_error(self):
        stderr = io.StringIO()
        error = BackendInstanceLockError(
            "backend_already_running",
            "Another backend already owns this configuration.",
            details={"lock_path": "backend.lock"},
        )
        with (
            patch.dict(os.environ, {"ZVEC_BACKEND_TOKEN": "secret"}),
            patch(
                "image_vector_service.backend_server.serve_backend",
                side_effect=error,
            ),
            redirect_stderr(stderr),
        ):
            exit_code = image_service.main(["serve"])

        self.assertEqual(exit_code, 6)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload["event"], "backend_start_failed")
        self.assertEqual(payload["error"]["code"], "backend_already_running")

    def test_serve_subcommand_uses_environment_token(self):
        workspace = Path(tempfile.mkdtemp(prefix="zvec_serve_cli_test_"))
        try:
            with (
                patch.dict(os.environ, {"ZVEC_BACKEND_TOKEN": "secret"}),
                patch(
                    "image_vector_service.backend_server.serve_backend"
                ) as serve_backend,
            ):
                exit_code = image_service.main(
                    [
                        "--workspace",
                        str(workspace),
                        "serve",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "8765",
                        "--query-root",
                        str(workspace),
                        "--libraries-config",
                        str(workspace / "libraries.json"),
                        "--instance-id",
                        "desktop-instance",
                        "--config-fingerprint",
                        "a" * 64,
                        "--instance-lock-path",
                        str(workspace / "backend.lock"),
                    ]
                )
            self.assertEqual(exit_code, 0)
            kwargs = serve_backend.call_args.kwargs
            self.assertEqual(kwargs["host"], "0.0.0.0")
            self.assertEqual(kwargs["port"], 8765)
            self.assertEqual(kwargs["token"], "secret")
            self.assertEqual(kwargs["query_root"], str(workspace))
            self.assertEqual(
                kwargs["libraries_config"], str(workspace / "libraries.json")
            )
            self.assertEqual(kwargs["instance_id"], "desktop-instance")
            self.assertEqual(kwargs["config_fingerprint"], "a" * 64)
            self.assertEqual(
                kwargs["instance_lock_path"], str(workspace / "backend.lock")
            )
            self.assertEqual(kwargs["config"].workspace, workspace.resolve())
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
