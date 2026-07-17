from __future__ import annotations

import hashlib
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from image_vector_service.config import ServiceConfig
from image_vector_service.metadata_backfill import (
    MetadataBackfillItem,
    MetadataBackfillRunner,
)
from image_vector_service.metadata_text import build_metadata_text
from image_vector_service.service import ImageVectorService


@dataclass
class _Response:
    vectors: list[list[float]]
    request_id: str = ""
    usage: dict[str, int] | None = None

    def __post_init__(self) -> None:
        self.usage = dict(self.usage or {})


def _item(doc_id: str, text: str | None = None) -> MetadataBackfillItem:
    value = text or f"description for {doc_id}"
    return MetadataBackfillItem(
        doc_id=doc_id,
        text=value,
        text_hash=hashlib.sha256(value.encode("utf-8")).hexdigest(),
    )


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_text(self, text: str) -> _Response:
        self.calls.append(text)
        if text == "embedding failure":
            raise RuntimeError("injected provider failure")
        return _Response(
            vectors=[[0.1, 0.2, 0.3]],
            request_id=f"request-{len(self.calls)}",
            usage={"input_tokens": len(text)},
        )


class MetadataBackfillRunnerTest(unittest.TestCase):
    def test_item_failures_are_skipped_and_later_items_commit(self) -> None:
        client = _RecordingClient()
        committed: list[str] = []

        def commit(item: MetadataBackfillItem, _vector: list[float]) -> None:
            if item.doc_id == "commit-failure":
                raise OSError("injected write failure")
            committed.append(item.doc_id)

        report = MetadataBackfillRunner(
            client=client,
            commit=commit,
            expected_dimension=3,
        ).run(
            [
                _item("first"),
                _item("embedding-failure", "embedding failure"),
                _item("commit-failure"),
                _item("last"),
            ]
        )

        self.assertCountEqual(committed, ["first", "last"])
        self.assertEqual(report.attempted, 4)
        self.assertEqual(report.api_requests, 4)
        self.assertEqual(report.succeeded, 2)
        self.assertEqual(report.failed, 2)
        self.assertEqual(report.remaining, 2)
        self.assertCountEqual(
            [(failure.doc_id, failure.stage) for failure in report.failures],
            [
                ("embedding-failure", "embedding"),
                ("commit-failure", "commit"),
            ],
        )

    def test_changed_text_is_deferred_without_committing_stale_vector(self) -> None:
        committed: list[str] = []
        item = _item("changed")
        report = MetadataBackfillRunner(
            client=_RecordingClient(),
            commit=lambda candidate, _vector: committed.append(candidate.doc_id),
            expected_dimension=3,
            still_current=lambda _candidate: False,
        ).run([item])

        self.assertEqual(committed, [])
        self.assertEqual(report.attempted, 1)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.changed_during_run, 1)
        self.assertEqual(report.remaining, 1)

    def test_invalid_response_is_an_item_failure_not_a_job_failure(self) -> None:
        class InvalidThenValidClient:
            def embed_text(self, text: str) -> _Response:
                dimension = 2 if "invalid" in text else 3
                return _Response(vectors=[[0.1] * dimension])

        committed: list[str] = []
        report = MetadataBackfillRunner(
            client=InvalidThenValidClient(),
            commit=lambda item, _vector: committed.append(item.doc_id),
            expected_dimension=3,
        ).run([_item("invalid"), _item("valid")])

        self.assertEqual(committed, ["valid"])
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.failures[0].stage, "response")
        self.assertEqual(report.succeeded, 1)

    def test_network_requests_are_bounded_and_commits_remain_serial(self) -> None:
        lock = threading.Lock()
        first_wave_ready = threading.Event()
        active_requests = 0
        maximum_active_requests = 0
        active_commits = 0
        maximum_active_commits = 0
        call_count = 0
        committed: list[str] = []

        class ConcurrentClient:
            def embed_text(self, _text: str) -> _Response:
                nonlocal active_requests
                nonlocal call_count
                nonlocal maximum_active_requests
                with lock:
                    call_count += 1
                    active_requests += 1
                    maximum_active_requests = max(
                        maximum_active_requests,
                        active_requests,
                    )
                    if active_requests == 2:
                        first_wave_ready.set()
                if not first_wave_ready.wait(timeout=1):
                    raise TimeoutError("second metadata worker did not start")
                with lock:
                    active_requests -= 1
                return _Response(vectors=[[0.1, 0.2, 0.3]])

        def commit(item: MetadataBackfillItem, _vector: list[float]) -> None:
            nonlocal active_commits
            nonlocal maximum_active_commits
            with lock:
                active_commits += 1
                maximum_active_commits = max(maximum_active_commits, active_commits)
            try:
                committed.append(item.doc_id)
            finally:
                with lock:
                    active_commits -= 1

        report = MetadataBackfillRunner(
            client=ConcurrentClient(),
            commit=commit,
            expected_dimension=3,
            concurrency=2,
        ).run([_item(f"item-{index}") for index in range(6)])

        self.assertEqual(call_count, 6)
        self.assertEqual(maximum_active_requests, 2)
        self.assertEqual(maximum_active_commits, 1)
        self.assertEqual(report.concurrency, 2)
        self.assertEqual(report.succeeded, 6)
        self.assertCountEqual(
            committed,
            [f"item-{index}" for index in range(6)],
        )

    def test_cancellation_does_not_wait_for_late_network_result(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()
        client_cancelled = threading.Event()
        committed: list[str] = []

        class BlockingClient:
            def embed_text(self, _text: str) -> _Response:
                started.set()
                while not release.is_set() and not client_cancelled.wait(timeout=0.01):
                    pass
                return _Response(vectors=[[0.1, 0.2, 0.3]])

        class ExpectedCancellation(RuntimeError):
            pass

        def cancel_check() -> None:
            if cancelled.is_set():
                raise ExpectedCancellation("cancel requested")

        def request_cancel() -> None:
            if started.wait(timeout=1):
                cancelled.set()

        canceller = threading.Thread(target=request_cancel, daemon=True)
        canceller.start()
        started_at = time.monotonic()
        try:
            with self.assertRaisesRegex(ExpectedCancellation, "cancel requested"):
                MetadataBackfillRunner(
                    client=BlockingClient(),
                    commit=lambda item, _vector: committed.append(item.doc_id),
                    expected_dimension=3,
                    cancel_check=cancel_check,
                    client_cancel_event=client_cancelled,
                    cancellation_poll_seconds=0.01,
                ).run([_item("slow")])
            self.assertLess(time.monotonic() - started_at, 0.5)
            self.assertTrue(client_cancelled.is_set())
            self.assertEqual(committed, [])
        finally:
            release.set()
            canceller.join(timeout=1)

    def test_cancellation_does_not_submit_beyond_the_active_wave(self) -> None:
        started = threading.Condition()
        started_count = 0
        cancelled = threading.Event()
        client_cancelled = threading.Event()
        committed: list[str] = []

        class BlockingClient:
            def embed_text(self, _text: str) -> _Response:
                nonlocal started_count
                with started:
                    started_count += 1
                    started.notify_all()
                client_cancelled.wait(timeout=2)
                return _Response(vectors=[[0.1, 0.2, 0.3]])

        class ExpectedCancellation(RuntimeError):
            pass

        def cancel_check() -> None:
            if cancelled.is_set():
                raise ExpectedCancellation("cancel requested")

        def request_cancel() -> None:
            with started:
                if started.wait_for(lambda: started_count == 2, timeout=1):
                    cancelled.set()

        canceller = threading.Thread(target=request_cancel, daemon=True)
        canceller.start()
        try:
            with self.assertRaisesRegex(ExpectedCancellation, "cancel requested"):
                MetadataBackfillRunner(
                    client=BlockingClient(),
                    commit=lambda item, _vector: committed.append(item.doc_id),
                    expected_dimension=3,
                    cancel_check=cancel_check,
                    client_cancel_event=client_cancelled,
                    concurrency=2,
                    cancellation_poll_seconds=0.01,
                ).run([_item(f"item-{index}") for index in range(5)])
            self.assertTrue(client_cancelled.is_set())
            self.assertEqual(started_count, 2)
            self.assertEqual(committed, [])
        finally:
            client_cancelled.set()
            canceller.join(timeout=1)

    def test_concurrency_must_be_a_positive_integer(self) -> None:
        with self.assertRaisesRegex(ValueError, "concurrency"):
            MetadataBackfillRunner(
                client=_RecordingClient(),
                commit=lambda _item, _vector: None,
                expected_dimension=3,
                concurrency=0,
            )

    def test_report_records_resumable_deferred_work(self) -> None:
        report = MetadataBackfillRunner(
            client=_RecordingClient(),
            commit=lambda _item, _vector: None,
            expected_dimension=3,
        ).run([_item("one")], eligible=3)

        payload = report.to_dict()
        self.assertTrue(payload["explicit_trigger"])
        self.assertTrue(payload["resumable"])
        self.assertEqual(payload["selected"], 1)
        self.assertEqual(payload["concurrency"], 1)
        self.assertEqual(payload["deferred"], 2)
        self.assertEqual(payload["remaining"], 2)


class _State:
    def __init__(self, entries: list[dict], annotations: dict[str, dict]) -> None:
        self.entries = {str(entry["doc_id"]): entry for entry in entries}
        self.annotations = annotations

    def list_entries(self) -> list[dict]:
        return list(self.entries.values())

    def get(self, doc_id: str) -> dict | None:
        return self.entries.get(doc_id)

    def get_document_annotation(self, doc_id: str) -> dict | None:
        return self.annotations.get(doc_id)


class _Repository:
    def __init__(self, stored: dict[str, dict]) -> None:
        self.stored = stored
        self.upserts: list[str] = []

    def fetch_metadata(self, doc_id: str) -> dict | None:
        return self.stored.get(doc_id)

    def upsert_metadata_embedding(
        self,
        doc_id: str,
        metadata_text: str,
        metadata_text_hash: str,
        vector: list[float],
    ) -> None:
        self.upserts.append(doc_id)
        self.stored[doc_id] = {
            "metadata_text": metadata_text,
            "metadata_text_hash": metadata_text_hash,
            "metadata_embedding": list(vector),
        }


class _Logger:
    def info(self, _message: str, *_args: object) -> None:
        pass


class ImageVectorServiceMetadataBackfillTest(unittest.TestCase):
    def _service(self) -> tuple[ImageVectorService, _Repository, _RecordingClient]:
        entries = [
            {
                "doc_id": "pending",
                "tags": ["portrait"],
                "folder_tags": [],
                "accepted_auto_tags": [],
            },
            {
                "doc_id": "current",
                "tags": ["cosplay"],
                "folder_tags": [],
                "accepted_auto_tags": [],
            },
            {
                "doc_id": "empty",
                "tags": [],
                "folder_tags": [],
                "accepted_auto_tags": [],
            },
        ]
        state = _State(entries, {})
        current_text = build_metadata_text(entries[1], None)
        repository = _Repository(
            {
                "pending": {
                    "metadata_text": "",
                    "metadata_text_hash": "",
                    "metadata_embedding": [0.0] * 3,
                },
                "current": {
                    "metadata_text": current_text.text,
                    "metadata_text_hash": current_text.sha256,
                    "metadata_embedding": [0.1] * 3,
                },
                "empty": {
                    "metadata_text": "",
                    "metadata_text_hash": "",
                    "metadata_embedding": [0.0] * 3,
                },
            }
        )
        client = _RecordingClient()
        service = object.__new__(ImageVectorService)
        service.config = ServiceConfig(workspace=Path.cwd())
        object.__setattr__(service.config, "dimension", 3)
        service.state = state
        service.repository = repository
        service._embedding_client = client
        service.progress = lambda _message: None
        service.cancel_check = lambda: None
        service.logger = _Logger()
        return service, repository, client

    def test_service_skips_current_and_empty_then_resumes_without_api_calls(
        self,
    ) -> None:
        service, repository, client = self._service()

        first = service.backfill_metadata_embeddings(max_images=1)
        self.assertEqual(first["scanned"], 3)
        self.assertEqual(first["selected"], 1)
        self.assertEqual(first["succeeded"], 1)
        self.assertEqual(first["already_current"], 1)
        self.assertEqual(first["skipped_empty"], 1)
        self.assertEqual(repository.upserts, ["pending"])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(first["rate_limit"]["backfill_concurrency"], 1)
        self.assertEqual(first["rate_limit"]["configured_backfill_concurrency"], 2)
        self.assertEqual(first["rate_limit"]["hard_requests_per_minute"], 60)
        self.assertEqual(first["rate_limit"]["hard_tokens_per_minute"], 100_000)

        second = service.backfill_metadata_embeddings(max_images=1)
        self.assertEqual(second["selected"], 0)
        self.assertEqual(second["succeeded"], 0)
        self.assertEqual(second["already_current"], 2)
        self.assertEqual(second["remaining"], 0)
        self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
