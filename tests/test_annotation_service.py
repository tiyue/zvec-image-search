from __future__ import annotations

import gc
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from image_vector_service.annotation_service import AutoTaggingRequestError, _cache_key
from image_vector_service.auto_tag_cache import SharedAutoTagCache
from image_vector_service.auto_tagging_assets import FIELD_SPECS, FIELD_TAG_LABELS
from image_vector_service.config import ServiceConfig
from image_vector_service.dashscope_client import EmbeddingResponse
from image_vector_service.service import ImageVectorService
from image_vector_service.vision_tagging_client import (
    TaggingContext,
    VisionTaggingResponse,
    VisionTransportError,
    VisionUsage,
)

FLASH_MODEL = "qwen3-vl-flash"
PLUS_MODEL = "qwen3-vl-plus"
LABEL_CODES = {label: code for code, label in FIELD_TAG_LABELS.items()}


def annotation_payload(
    *,
    controlled_tags: tuple[str, ...] = ("Cosplay",),
    field_values: dict[str, tuple[str, ...]] | None = None,
    field_confidences: dict[str, float] | None = None,
    character: str | None = "刻晴",
    character_state: str = "confirmed",
    character_confidence: float | None = 0.95,
    character_evidence: tuple[str, ...] = ("visual",),
    work: str | None = "原神",
    work_confidence: float | None = 0.96,
    work_evidence: tuple[str, ...] = ("visual",),
    plus_recommended: bool = False,
    requires_review: bool = False,
    review_reasons: tuple[str, ...] = (),
    extra_entities: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    entities: dict[str, list[dict[str, Any]]] = {
        "real_person": [],
        "cosplayer": [],
        "character": [],
        "work": [],
    }
    if character is not None:
        entities["character"].append(
            {
                "name": character,
                "state": character_state,
                "evidence": list(character_evidence),
                "evidence_text": "",
                "confidence": character_confidence,
            }
        )
    if work is not None:
        entities["work"].append(
            {
                "name": work,
                "state": "confirmed",
                "evidence": list(work_evidence),
                "evidence_text": "",
                "confidence": work_confidence,
            }
        )
    for entity_type, entity_values in (extra_entities or {}).items():
        entities.setdefault(entity_type, []).extend(entity_values)
    selected_values: dict[str, list[str]] = {
        field_name: [] for field_name in FIELD_SPECS
    }
    for label in controlled_tags:
        code = LABEL_CODES[label]
        owner = next(
            field_name
            for field_name, spec in FIELD_SPECS.items()
            if code in spec.values
        )
        selected_values[owner].append(code)
    for field_name, values in (field_values or {}).items():
        selected_values[field_name] = list(values)
    fields: dict[str, dict[str, Any]] = {
        field_name: {
            "values": values,
            "labels": [FIELD_TAG_LABELS[code] for code in values],
            "confidence": (field_confidences or {}).get(
                field_name,
                0.98 if values else 0.0,
            ),
        }
        for field_name, values in selected_values.items()
    }
    labels = [label for field in fields.values() for label in field["labels"]]
    return {
        "schema_version": 2,
        "prompt_version": "people-cosplay-v2",
        "description": "刻晴Cosplay",
        "fields": fields,
        "categories": {
            field_name: list(field["labels"]) for field_name, field in fields.items()
        },
        "controlled_tags": labels,
        "entities": entities,
        "suggested_tags": ["低置信自由标签"],
        "requires_review": requires_review,
        "review_reasons": list(review_reasons),
        "plus_recommended": plus_recommended,
        "warnings": [],
    }


class FakeEmbeddingClient:
    def __init__(self, dimension: int = 1024):
        self.dimension = dimension
        self.request_count = 0

    def embed_images(self, paths):
        self.request_count += 1
        return EmbeddingResponse(
            vectors=[[1.0] + [0.0] * (self.dimension - 1) for _ in paths],
            request_id=f"image-{self.request_count}",
            usage={},
        )

    def embed_text(self, _text):
        self.request_count += 1
        return EmbeddingResponse(
            vectors=[[1.0] + [0.0] * (self.dimension - 1)],
            request_id=f"text-{self.request_count}",
            usage={},
        )


class FakeVisionClient:
    calls = 0
    contexts: list[TaggingContext] = []
    models: list[str] = []
    responses: dict[str, list[Any]] = {}

    def __init__(self, config, *, budget_tracker=None):
        self.config = config
        self.model = config.model
        self.budget_tracker = budget_tracker

    def tag_image(self, _path, *, context):
        type(self).calls += 1
        type(self).contexts.append(context)
        type(self).models.append(self.model)
        configured = type(self).responses.get(self.model, [])
        response_value = configured.pop(0) if configured else annotation_payload()
        if isinstance(response_value, Exception):
            raise response_value
        usage = VisionUsage(1000, 100, 1100, {"prompt_tokens": 1000})
        if self.budget_tracker is not None:
            self.budget_tracker.authorize_next_image()
            self.budget_tracker.record(usage)
        cost = (
            self.config.pricing.cost(usage.input_tokens, usage.output_tokens)
            if self.config.pricing is not None
            else None
        )
        return VisionTaggingResponse(
            response_value,
            f"vision-{self.model}-{type(self).calls}",
            usage,
            cost,
        )


class ConcurrentVisionClient(FakeVisionClient):
    active = 0
    max_active = 0
    failed_names: set[str] = set()
    lock = threading.Lock()

    def tag_image(self, path, *, context):
        with type(self).lock:
            type(self).active += 1
            type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            time.sleep(0.04)
            if Path(path).name in type(self).failed_names:
                raise RuntimeError("synthetic per-image failure")
            return super().tag_image(path, context=context)
        finally:
            with type(self).lock:
                type(self).active -= 1


class BlockingVisionClient(FakeVisionClient):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.started = threading.Event()
        cls.release = threading.Event()
        cls.finished = threading.Event()

    def tag_image(self, path, *, context):
        type(self).started.set()
        try:
            if not type(self).release.wait(timeout=5):
                raise TimeoutError("test did not release visual request")
            return super().tag_image(path, context=context)
        finally:
            type(self).finished.set()


class PipelineCancelled(RuntimeError):
    pass


class FailingVisionClient:
    def __init__(self, _config, *, budget_tracker=None):
        self.budget_tracker = budget_tracker

    def tag_image(self, _path, *, context):
        raise VisionTransportError("temporary visual service failure")


class RetryableVisionClient:
    def __init__(self, _config, *, budget_tracker=None):
        self.budget_tracker = budget_tracker
        self.request_count = 0

    def tag_image(self, _path, *, context):
        self.request_count += 1
        raise VisionTransportError(
            "model rate limited",
            status_code=429,
            code="RateLimit",
        )


class SystemicVisionClient:
    def __init__(self, _config, *, budget_tracker=None):
        self.budget_tracker = budget_tracker
        self.request_count = 0

    def tag_image(self, _path, *, context):
        self.request_count += 1
        raise VisionTransportError(
            "invalid API credential",
            status_code=401,
            code="InvalidApiKey",
        )


class AutoTaggingIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(tempfile.mkdtemp(prefix="zvec_auto_tag_"))
        self.root = self.temporary / "library"
        first = self.root / "set-a"
        second = self.root / "set-b"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        image = first / "image.png"
        Image.new("RGB", (24, 24), (120, 80, 180)).save(image)
        shutil.copy2(image, second / "copy.png")
        self.service = ImageVectorService(
            config=ServiceConfig(workspace=self.temporary / "workspace"),
            embedding_client=FakeEmbeddingClient(),
        )
        FakeVisionClient.calls = 0
        FakeVisionClient.contexts = []
        FakeVisionClient.models = []
        FakeVisionClient.responses = {}

    def tearDown(self):
        self.service.close()
        shutil.rmtree(self.temporary, ignore_errors=True)

    def test_same_sha_is_tagged_once_and_review_preserves_tag_sources(self):
        indexed = self.service.index_folder(str(self.root), tags=["人工"])
        self.assertEqual(indexed.inserted, 2)
        estimate = self.service.estimate_auto_tags(
            scope="latest_index_run", max_images=10, max_budget_cny=1.0
        )
        self.assertEqual(estimate["candidate_count"], 2)
        self.assertEqual(estimate["api_request_count"], 1)

        with self.assertRaises(AutoTaggingRequestError):
            self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=10,
                external_processing_confirmed=False,
            )

        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=10,
                max_budget_cny=1.0,
                external_processing_confirmed=True,
            )

        self.assertEqual(FakeVisionClient.calls, 1)
        self.assertEqual(report["succeeded"], 2)
        self.assertEqual(len(report["proposals"]), 2)
        self.assertEqual(
            report["proposals"][0]["entities"]["character"][0]["name"], "刻晴"
        )
        self.assertEqual(report["api_request_count"], 1)
        self.assertEqual(report["flash_requests"], 1)
        self.assertEqual(report["plus_requests"], 0)
        self.assertEqual(report["proposals"][0]["model_trace"], [FLASH_MODEL])
        self.assertEqual(report["proposals"][0]["resolved_model"], FLASH_MODEL)
        self.assertIn("Cosplay", report["proposals"][0]["proposed_tags"])
        self.assertNotIn("低置信自由标签", report["proposals"][0]["proposed_tags"])
        self.assertEqual(
            report["proposals"][0]["proposal_id"], report["proposals"][0]["doc_id"]
        )
        self.assertIn("set-a", FakeVisionClient.contexts[0].folder_name)
        self.assertIn("set-b", FakeVisionClient.contexts[0].folder_name)

        proposal = report["proposals"][0]
        source_path = Path(proposal["source_path"])
        self.assertTrue(source_path.is_file())
        reviewed = self.service.review_auto_tags(
            [
                {
                    "doc_id": proposal["doc_id"],
                    "action": "accept",
                    "tags": ["Cosplay"],
                }
            ]
        )
        self.assertEqual(reviewed["accepted"], 1)
        entry = self.service.state.get(proposal["doc_id"])
        assert entry is not None
        self.assertEqual(entry["tags"], ["人工"])
        self.assertEqual(
            entry["accepted_auto_tags"],
            ["刻晴", "原神", "Cosplay"],
        )
        self.assertIn(entry["folder_tags"][0], {"set-a", "set-b"})
        stored = self.service.state.get_document_annotation(proposal["doc_id"])
        assert stored is not None
        self.assertEqual(stored["policy"]["resolved_model"], FLASH_MODEL)
        self.assertIn("fields", stored["structured"])

        search = self.service.search_by_text("portrait", tags=["神"], top_k=10)
        self.assertEqual(search.result_count, 1)
        self.assertIn("原神", search.results[0].tags)
        self.assertEqual(
            self.service.estimate_auto_tags(scope="untagged")["candidate_count"],
            0,
        )

    def test_auto_tag_network_requests_are_bounded_and_one_failure_continues(self):
        concurrent_root = self.root / "concurrent"
        concurrent_root.mkdir()
        for index, color in enumerate(
            ((220, 20, 20), (20, 220, 20), (20, 20, 220), (180, 120, 40))
        ):
            Image.new("RGB", (32, 32), color).save(
                concurrent_root / f"parallel-{index}.png"
            )
        failed_name = "parallel-2.png"
        indexed = self.service.index_folder(str(self.root))
        self.assertEqual(indexed.inserted, 6)

        ConcurrentVisionClient.active = 0
        ConcurrentVisionClient.max_active = 0
        ConcurrentVisionClient.failed_names = {failed_name}
        ConcurrentVisionClient.calls = 0
        ConcurrentVisionClient.contexts = []
        ConcurrentVisionClient.models = []
        ConcurrentVisionClient.responses = {}
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            ConcurrentVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=20,
                max_budget_cny=5.0,
                external_processing_confirmed=True,
            )

        self.assertGreaterEqual(ConcurrentVisionClient.max_active, 2)
        self.assertLessEqual(
            ConcurrentVisionClient.max_active,
            self.service.config.auto_tag_concurrency,
        )
        self.assertEqual(report["candidate_count"], 6)
        self.assertEqual(report["unique_image_count"], 5)
        self.assertEqual(report["succeeded"], 5)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["api_request_count"], 5)
        self.assertEqual(self.service.stats()["tracked_files"], 6)
        self.assertFalse(report["needs_attention"])
        self.assertEqual(report["quarantined"], 1)
        self.assertEqual(report["quarantine_copy_failures"], 0)
        manifest_path = Path(report["failure_manifest"])
        self.assertTrue(manifest_path.is_file())
        manifest_entries = [
            json.loads(line)
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(manifest_entries), 1)
        self.assertEqual(manifest_entries[0]["kind"], "item")
        self.assertEqual(manifest_entries[0]["stage"], "auto_tag")
        self.assertEqual(Path(manifest_entries[0]["source_path"]).name, failed_name)
        self.assertTrue(Path(manifest_entries[0]["blob_path"]).is_file())
        self.assertEqual(len(report["failures"]), 1)

    def test_auto_tag_cancellation_does_not_wait_for_running_network_call(self):
        # Four unique SHA groups fill two running and two queued worker slots.
        # Cancellation must retire every claim without waiting for running HTTP.
        for index, color in enumerate(((10, 20, 30), (40, 50, 60), (70, 80, 90))):
            Image.new("RGB", (24, 24), color).save(
                self.root / "set-a" / f"cancel-{index}.png"
            )
        self.service.index_folder(str(self.root))
        cancel_requested = threading.Event()

        def cancel_check() -> None:
            if cancel_requested.is_set():
                raise PipelineCancelled("cancel requested")

        # ImageVectorService and its coordinator retain the callback
        # separately, so update both for this post-index cancellation test.
        self.service.cancel_check = cancel_check
        self.service.auto_tagging.cancel_check = cancel_check
        BlockingVisionClient.reset()

        def request_cancel_after_start() -> None:
            if BlockingVisionClient.started.wait(timeout=2):
                cancel_requested.set()

        canceller = threading.Thread(
            target=request_cancel_after_start,
            daemon=True,
        )
        canceller.start()
        started_at = time.monotonic()
        try:
            with (
                patch(
                    "image_vector_service.annotation_service."
                    "DashScopeVisionTaggingClient",
                    BlockingVisionClient,
                ),
                self.assertRaisesRegex(PipelineCancelled, "cancel requested"),
            ):
                self.service.auto_tag_images(
                    scope="latest_index_run",
                    max_images=10,
                    external_processing_confirmed=True,
                )

            elapsed = time.monotonic() - started_at
            self.assertLess(elapsed, 1.0)
            self.assertFalse(BlockingVisionClient.finished.is_set())
            self.assertEqual(
                self.service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache"
                ).fetchone()[0],
                0,
            )
            entries = self.service.state.list_entries()
            self.assertTrue(entries)
            cache_keys = {
                _cache_key(str(entry["sha256"]), FLASH_MODEL) for entry in entries
            }
            self.assertEqual(len(cache_keys), 4)
            running_claims = 0
            released_queued_claims = 0
            for cache_key in cache_keys:
                while_worker_runs = self.service.auto_tag_cache.claim(cache_key)
                if while_worker_runs.is_leader:
                    # cancel_futures=True retires queued work immediately.
                    released_queued_claims += 1
                    while_worker_runs.release()
                else:
                    # Calls already inside the model client keep their claims
                    # until their done callbacks run.
                    running_claims += 1
            self.assertGreaterEqual(released_queued_claims, 1)
            self.assertGreaterEqual(running_claims, 1)
            for entry in entries:
                self.assertIsNone(
                    self.service.state.get_document_annotation(str(entry["doc_id"]))
                )

            # A late worker completion remains detached from cache/state.
            BlockingVisionClient.release.set()
            self.assertTrue(BlockingVisionClient.finished.wait(timeout=2))
            for cache_key in cache_keys:
                release_deadline = time.monotonic() + 2
                while True:
                    after_worker_exit = self.service.auto_tag_cache.claim(cache_key)
                    if after_worker_exit.is_leader:
                        break
                    if time.monotonic() >= release_deadline:
                        self.fail(
                            "cancelled model flight was not released after worker exit"
                        )
                    time.sleep(0.01)
                after_worker_exit.release()
            self.assertEqual(
                self.service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache"
                ).fetchone()[0],
                0,
            )
            for entry in entries:
                self.assertIsNone(
                    self.service.state.get_document_annotation(str(entry["doc_id"]))
                )
        finally:
            BlockingVisionClient.release.set()

    def test_same_size_source_replacement_is_retryable_and_never_sent(self):
        root = self.temporary / "sha-library"
        root.mkdir()
        source = root / "source.bmp"
        Image.new("RGB", (32, 24), (10, 20, 30)).save(source)
        service = ImageVectorService(
            config=ServiceConfig(workspace=self.temporary / "sha-workspace"),
            embedding_client=FakeEmbeddingClient(),
        )
        try:
            indexed = service.index_folder(str(root))
            self.assertEqual(indexed.inserted, 1)
            original_payload = source.read_bytes()
            original_hash = hashlib.sha256(original_payload).hexdigest()
            original_stat = source.stat()

            Image.new("RGB", (32, 24), (220, 210, 200)).save(source)
            os.utime(
                source,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            self.assertEqual(source.stat().st_size, len(original_payload))
            self.assertEqual(source.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertNotEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(), original_hash
            )

            report = service.auto_tag_images(
                scope="latest_index_run",
                max_images=10,
                external_processing_confirmed=True,
            )

            self.assertEqual(report["api_request_count"], 0)
            self.assertEqual(report["succeeded"], 0)
            self.assertEqual(report["failed"], 1)
            self.assertEqual(report["quarantined"], 0)
            self.assertFalse(report["needs_attention"])
            self.assertEqual(
                service.auto_tag_cache.connection.execute(
                    "SELECT COUNT(*) FROM auto_tag_cache"
                ).fetchone()[0],
                0,
            )
            after_source_change = service.auto_tag_cache.claim(
                _cache_key(original_hash, FLASH_MODEL)
            )
            self.assertTrue(after_source_change.is_leader)
            after_source_change.release()
            manifest = Path(report["failure_manifest"])
            failure = json.loads(manifest.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(failure["kind"], "retryable")
            self.assertEqual(failure["blob_path"], "")
        finally:
            service.close()

    def test_path_preparation_failure_releases_single_flight_claim(self):
        self.service.index_folder(str(self.root))
        entry = self.service.state.list_entries()[0]
        cache_key = _cache_key(str(entry["sha256"]), FLASH_MODEL)

        with patch.object(
            self.service.auto_tagging.source_resolver,
            "resolve_fields",
            side_effect=OSError("injected source resolution failure"),
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )

        self.assertEqual(report["api_request_count"], 0)
        self.assertGreaterEqual(report["failed"], 1)
        after_failure = self.service.auto_tag_cache.claim(cache_key)
        self.assertTrue(after_failure.is_leader)
        after_failure.release()

    def test_cache_persistence_exception_releases_single_flight_claim(self):
        self.service.index_folder(str(self.root))
        entry = self.service.state.list_entries()[0]
        cache_key = _cache_key(str(entry["sha256"]), FLASH_MODEL)

        with (
            patch(
                "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
                FakeVisionClient,
            ),
            patch.object(
                self.service.auto_tag_cache,
                "set",
                side_effect=RuntimeError("injected cache persistence failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "cache persistence failure"),
        ):
            self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )

        after_exception = self.service.auto_tag_cache.claim(cache_key)
        self.assertTrue(after_exception.is_leader)
        after_exception.release()

    def test_visual_failure_is_cached_without_rolling_back_index(self):
        indexed = self.service.index_folder(str(self.root))
        self.assertEqual(indexed.inserted, 2)

        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FailingVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=10,
                external_processing_confirmed=True,
            )

        self.assertEqual(report["failed"], 2)
        self.assertEqual(self.service.stats()["tracked_files"], 2)
        retry = self.service.estimate_auto_tags(scope="failed", max_images=10)
        self.assertEqual(retry["candidate_count"], 2)
        self.assertEqual(retry["api_request_count"], 0)

        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            cached_failure = self.service.auto_tag_images(
                scope="failed",
                max_images=10,
                external_processing_confirmed=True,
            )
        self.assertEqual(FakeVisionClient.calls, 0)
        self.assertEqual(cached_failure["failed"], 2)
        self.assertEqual(cached_failure["api_request_count"], 0)

    def test_failed_annotations_can_be_completed_with_manual_tags(self):
        self.service.index_folder(str(self.root))
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FailingVisionClient,
        ):
            self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=10,
                external_processing_confirmed=True,
            )

        failed_page = self.service.pending_auto_tags(
            limit=10,
            filters={"review_state": "failed"},
        )
        self.assertEqual(failed_page["pending_count"], 2)
        proposal = failed_page["proposals"][0]
        self.assertEqual(proposal["status"], "failed")
        self.assertIn("temporary visual service failure", proposal["error"])

        completed = self.service.review_auto_tags(
            [
                {
                    "doc_id": proposal["doc_id"],
                    "action": "manual",
                    "tags": ["人工补标", "角色A"],
                }
            ]
        )

        self.assertEqual(completed["accepted"], 1)
        entry = self.service.state.get(proposal["doc_id"])
        annotation = self.service.state.get_document_annotation(proposal["doc_id"])
        assert entry is not None
        assert annotation is not None
        self.assertEqual(entry["tags"], ["人工补标", "角色A"])
        self.assertEqual(entry["accepted_auto_tags"], [])
        self.assertEqual(annotation["status"], "accepted")
        self.assertTrue(annotation["policy"]["manually_resolved"])
        self.assertIn("temporary visual service failure", annotation["error"])
        remaining = self.service.pending_auto_tags(
            limit=10,
            filters={"review_state": "failed"},
        )
        self.assertEqual(remaining["pending_count"], 1)

    def test_failed_manual_label_requires_a_non_empty_tag(self):
        self.service.index_folder(str(self.root))
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FailingVisionClient,
        ):
            self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=1,
                external_processing_confirmed=True,
            )
        proposal = self.service.pending_auto_tags(
            limit=10,
            filters={"review_state": "failed"},
        )["proposals"][0]

        result = self.service.review_auto_tags(
            [
                {
                    "doc_id": proposal["doc_id"],
                    "action": "manual",
                    "tags": [],
                }
            ]
        )

        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["failed"], 1)
        entry = self.service.state.get(proposal["doc_id"])
        annotation = self.service.state.get_document_annotation(proposal["doc_id"])
        assert entry is not None
        assert annotation is not None
        self.assertEqual(entry["tags"], [])
        self.assertEqual(annotation["status"], "failed")

    def test_content_policy_failures_are_not_selected_for_default_retry(self):
        self.service.index_folder(str(self.root))
        entry = self.service.state.list_entries()[0]
        self.service.state.set_document_annotation(
            doc_id=str(entry["doc_id"]),
            source_sha256=str(entry["sha256"]),
            cache_key=None,
            status="failed",
            error=(
                "DashScope data_inspection_failed: "
                "Input image data may contain inappropriate content."
            ),
            policy={},
        )

        retryable = self.service.estimate_auto_tags(scope="failed", max_images=10)
        all_failed = self.service.estimate_auto_tags(
            scope="failed_all",
            max_images=10,
        )

        self.assertEqual(retryable["candidate_count"], 0)
        self.assertEqual(all_failed["candidate_count"], 1)

    def test_desktop_library_scope_excludes_migrated_roots(self):
        active_root = self.temporary / "active-library"
        migrated_root = self.temporary / "migrated-library"
        active_root.mkdir()
        migrated_root.mkdir()
        Image.new("RGB", (18, 18), (20, 40, 60)).save(active_root / "active.png")
        Image.new("RGB", (18, 18), (60, 40, 20)).save(migrated_root / "old.png")
        scoped_service = ImageVectorService(
            config=ServiceConfig(
                workspace=self.temporary / "scoped-workspace",
                library_image_root=active_root,
            ),
            embedding_client=FakeEmbeddingClient(),
        )
        try:
            active_report = scoped_service.index_folder(str(active_root))
            scoped_service.index_folder(str(migrated_root))
            for entry in scoped_service.state.list_entries():
                scoped_service.state.set_document_annotation(
                    doc_id=str(entry["doc_id"]),
                    source_sha256=str(entry["sha256"]),
                    cache_key=None,
                    status="failed",
                    error="Model annotation schema is invalid.",
                )

            latest = scoped_service.estimate_auto_tags(
                scope="latest_index_run",
                max_images=10,
            )
            failed = scoped_service.estimate_auto_tags(
                scope="failed",
                max_images=10,
            )
            failed_page = scoped_service.pending_auto_tags(
                limit=10,
                filters={"review_state": "failed"},
            )
        finally:
            scoped_service.close()

        self.assertEqual(active_report.inserted, 1)
        self.assertEqual(latest["candidate_count"], 1)
        self.assertEqual(failed["candidate_count"], 1)
        self.assertEqual(failed_page["pending_count"], 1)
        self.assertEqual(
            failed_page["proposals"][0]["relative_path"],
            "active.png",
        )

    def test_provider_failures_are_audited_without_quarantining_healthy_images(
        self,
    ):
        indexed = self.service.index_folder(str(self.root))
        self.assertEqual(indexed.inserted, 2)

        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            RetryableVisionClient,
        ):
            retryable = self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=10,
                external_processing_confirmed=True,
            )

        self.assertEqual(retryable["failed"], 2)
        self.assertEqual(retryable["quarantined"], 0)
        self.assertFalse(retryable["needs_attention"])
        retryable_entries = [
            json.loads(line)
            for line in Path(retryable["failure_manifest"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(retryable_entries), 2)
        self.assertTrue(
            all(entry["kind"] == "retryable" for entry in retryable_entries)
        )
        self.assertTrue(all(entry["blob_path"] == "" for entry in retryable_entries))

        new_image = self.root / "systemic.png"
        Image.new("RGB", (20, 20), (10, 40, 90)).save(new_image)
        second_index = self.service.index_folder(str(self.root))
        self.assertEqual(second_index.inserted, 1)
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            SystemicVisionClient,
        ):
            systemic = self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=10,
                external_processing_confirmed=True,
            )

        self.assertEqual(systemic["failed"], 1)
        self.assertEqual(systemic["quarantined"], 0)
        self.assertTrue(systemic["needs_attention"])
        systemic_entries = [
            json.loads(line)
            for line in Path(systemic["failure_manifest"])
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(len(systemic_entries), 1)
        self.assertEqual(systemic_entries[0]["kind"], "systemic")
        self.assertEqual(systemic_entries[0]["blob_path"], "")
        blob_root = self.service.config.results_path / "failed-images" / "blobs"
        self.assertFalse(blob_root.exists())

    def test_scope_all_reuses_cache_without_repeating_requests(self):
        self.service.index_folder(str(self.root))
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            first = self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )
            second = self.service.auto_tag_images(
                scope="all",
                external_processing_confirmed=True,
            )

        self.assertEqual(first["api_request_count"], 1)
        self.assertEqual(second["api_request_count"], 0)
        self.assertEqual(second["flash_requests"], 0)
        self.assertEqual(second["plus_requests"], 0)
        self.assertEqual(second["cached"], 1)
        self.assertEqual(FakeVisionClient.calls, 1)

    def test_flash_escalates_only_when_recommended_and_merges_plus(self):
        self.service.index_folder(str(self.root))
        FakeVisionClient.responses = {
            FLASH_MODEL: [
                annotation_payload(
                    controlled_tags=("Cosplay",),
                    plus_recommended=True,
                    review_reasons=("flash_uncertain_pose",),
                )
            ],
            PLUS_MODEL: [
                annotation_payload(
                    controlled_tags=("全身",),
                    character_confidence=0.99,
                    work_confidence=0.99,
                )
            ],
        }
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                model="flash",
                external_processing_confirmed=True,
            )

        self.assertEqual(FakeVisionClient.models, [FLASH_MODEL, PLUS_MODEL])
        self.assertEqual(report["api_request_count"], 2)
        self.assertEqual(report["flash_requests"], 1)
        self.assertEqual(report["plus_requests"], 1)
        self.assertEqual(report["escalated_count"], 1)
        proposal = report["proposals"][0]
        self.assertEqual(proposal["model_trace"], [FLASH_MODEL, PLUS_MODEL])
        self.assertEqual(proposal["resolved_model"], PLUS_MODEL)
        self.assertTrue(proposal["escalated"])
        self.assertIn("Cosplay", proposal["proposed_tags"])
        self.assertIn("全身", proposal["proposed_tags"])
        self.assertEqual(
            set(proposal["auto_accepted_identity_tags"]),
            {"刻晴", "原神"},
        )
        self.assertNotIn("刻晴", proposal["proposed_tags"])
        self.assertNotIn("原神", proposal["proposed_tags"])
        self.assertEqual(
            proposal["fields"]["content_domain"]["labels"],
            ["Cosplay"],
        )
        self.assertEqual(
            proposal["fields"]["shot_type"]["labels"],
            ["全身"],
        )

    def test_field_merge_enforces_ownership_cardinality_and_conflict_audit(self):
        self.service.index_folder(str(self.root))
        flash_pose = FIELD_SPECS["pose"].values[:3]
        plus_pose = FIELD_SPECS["pose"].values[3:6]
        flash_action = FIELD_SPECS["action"].values[:1]
        plus_action = FIELD_SPECS["action"].values[1:2]
        FakeVisionClient.responses = {
            FLASH_MODEL: [
                annotation_payload(
                    field_values={
                        "people_count": ("count_single_person",),
                        "shot_type": ("shot_close_up",),
                        "pose": flash_pose,
                        "action": flash_action,
                    },
                    field_confidences={
                        "people_count": 0.90,
                        "shot_type": 0.88,
                        "pose": 0.80,
                        "action": 0.55,
                    },
                    plus_recommended=True,
                )
            ],
            PLUS_MODEL: [
                annotation_payload(
                    field_values={
                        "people_count": ("count_two_people",),
                        "shot_type": ("shot_full_body",),
                        "pose": plus_pose,
                        "action": plus_action,
                    },
                    field_confidences={
                        "people_count": 0.96,
                        "shot_type": 0.88,
                        "pose": 0.90,
                        "action": 0.95,
                    },
                )
            ],
        }

        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )

        proposal = report["proposals"][0]
        fields = proposal["fields"]
        self.assertEqual(set(fields), set(FIELD_SPECS))
        self.assertEqual(fields["people_count"]["values"], ["count_two_people"])
        # Equal confidence is deterministic (Plus wins) but remains auditable.
        self.assertEqual(fields["shot_type"]["values"], ["shot_full_body"])
        self.assertEqual(fields["pose"]["values"], list(plus_pose))
        self.assertEqual(
            set(fields["action"]["values"]),
            set(flash_action + plus_action),
        )
        self.assertEqual(fields["action"]["confidence"], 0.55)
        for field_name, field in fields.items():
            spec = FIELD_SPECS[field_name]
            self.assertLessEqual(len(field["values"]), spec.max_items)
            self.assertTrue(set(field["values"]).issubset(spec.values))
        self.assertNotIn(
            FIELD_TAG_LABELS["count_single_person"],
            proposal["proposed_tags"],
        )
        self.assertNotIn(
            FIELD_TAG_LABELS["shot_close_up"],
            proposal["proposed_tags"],
        )
        self.assertTrue(proposal["review_required"])
        for reason in (
            "field:people_count:conflict",
            "field:shot_type:conflict",
            "field:pose:conflict",
            "field:action:conflict",
            "field:pose:overflow",
        ):
            self.assertIn(reason, proposal["review_reasons"])

    def test_manual_plus_calls_only_plus(self):
        self.service.index_folder(str(self.root))
        FakeVisionClient.responses = {
            PLUS_MODEL: [annotation_payload(controlled_tags=("全身",))]
        }
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                model="plus",
                external_processing_confirmed=True,
            )

        self.assertEqual(FakeVisionClient.models, [PLUS_MODEL])
        self.assertEqual(report["flash_requests"], 0)
        self.assertEqual(report["plus_requests"], 1)
        self.assertEqual(report["escalated_count"], 0)
        self.assertEqual(report["proposals"][0]["resolved_model"], PLUS_MODEL)
        self.assertFalse(report["proposals"][0]["escalated"])

    def test_conflicting_and_low_confidence_entities_are_not_proposed(self):
        self.service.index_folder(str(self.root), tags=["identity-proof"])
        FakeVisionClient.responses = {
            FLASH_MODEL: [
                annotation_payload(
                    character="刻晴",
                    work=None,
                    plus_recommended=True,
                    extra_entities={
                        "real_person": [
                            {
                                "name": "明确人物",
                                "state": "confirmed",
                                "evidence": ["manual_tag"],
                                "evidence_text": "identity-proof",
                                "confidence": 0.95,
                            }
                        ],
                        "cosplayer": [
                            {
                                "name": "猜测Coser",
                                "state": "confirmed",
                                "evidence": ["visual"],
                                "confidence": 0.99,
                            }
                        ],
                    },
                )
            ],
            PLUS_MODEL: [
                annotation_payload(
                    character="甘雨",
                    work="原神",
                    work_confidence=0.60,
                )
            ],
        }
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )

        proposal = report["proposals"][0]
        self.assertIn("刻晴", proposal["proposed_tags"])
        self.assertIn("甘雨", proposal["proposed_tags"])
        self.assertNotIn("原神", proposal["proposed_tags"])
        self.assertIn("明确人物", proposal["auto_accepted_identity_tags"])
        self.assertNotIn("猜测Coser", proposal["proposed_tags"])
        character_entities = proposal["entities"]["character"]
        self.assertEqual(
            {item["name"] for item in character_entities},
            {"刻晴", "甘雨"},
        )
        self.assertTrue(all(item["state"] == "conflict" for item in character_entities))
        self.assertTrue(proposal["review_required"])
        self.assertIn("entity_conflict:character", proposal["review_reasons"])
        self.assertNotIn("low_confidence_entity:work", proposal["review_reasons"])
        self.assertIn(
            "entity:cosplayer:context_mismatch",
            proposal["review_reasons"],
        )

    def test_plus_escalation_cannot_exceed_total_budget(self):
        self.service.index_folder(str(self.root))
        FakeVisionClient.responses = {
            FLASH_MODEL: [annotation_payload(plus_recommended=True)],
            PLUS_MODEL: [annotation_payload(controlled_tags=("全身",))],
        }
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                max_budget_cny=0.001,
                external_processing_confirmed=True,
            )

        self.assertEqual(FakeVisionClient.models, [FLASH_MODEL])
        self.assertEqual(report["api_request_count"], 1)
        self.assertEqual(report["flash_requests"], 1)
        self.assertEqual(report["plus_requests"], 0)
        self.assertEqual(report["escalated_count"], 1)
        self.assertEqual(report["stopped_reason"], "budget_exhausted")
        self.assertLessEqual(report["actual_cost_cny"], 0.001)
        proposal = report["proposals"][0]
        self.assertEqual(proposal["resolved_model"], FLASH_MODEL)
        self.assertEqual(proposal["model_trace"], [FLASH_MODEL, PLUS_MODEL])
        self.assertIn("plus_budget_exhausted", proposal["review_reasons"])
        entry = self.service.state.list_entries()[0]
        after_budget_exit = self.service.auto_tag_cache.claim(
            _cache_key(str(entry["sha256"]), PLUS_MODEL)
        )
        self.assertTrue(after_budget_exit.is_leader)
        after_budget_exit.release()

    def test_pending_review_is_paginated_without_model_calls(self):
        self.service.index_folder(str(self.root))
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=10,
                external_processing_confirmed=True,
            )

        self.assertEqual(report["pending_count"], 2)
        calls_after_tagging = FakeVisionClient.calls
        first = self.service.pending_auto_tags(offset=0, limit=1)
        second = self.service.pending_auto_tags(offset=1, limit=1)
        beyond = self.service.pending_auto_tags(offset=2, limit=1)

        self.assertEqual(FakeVisionClient.calls, calls_after_tagging)
        self.assertEqual(first["pending_count"], 2)
        self.assertEqual(first["offset"], 0)
        self.assertEqual(first["limit"], 1)
        self.assertEqual(len(first["proposals"]), 1)
        self.assertTrue(first["has_more"])
        self.assertEqual(second["pending_count"], 2)
        self.assertEqual(len(second["proposals"]), 1)
        self.assertFalse(second["has_more"])
        self.assertNotEqual(
            first["proposals"][0]["doc_id"], second["proposals"][0]["doc_id"]
        )
        self.assertEqual(beyond["proposals"], [])
        self.assertFalse(beyond["has_more"])

        for offset, limit in ((-1, 1), (0, 0), (0, 501)):
            with self.assertRaises(AutoTaggingRequestError):
                self.service.pending_auto_tags(offset=offset, limit=limit)

    def test_auto_tag_terminal_response_caps_proposals_at_one_hundred(self):
        source = self.root / "set-a" / "image.png"
        for index in range(101):
            shutil.copy2(source, self.root / "set-a" / f"copy-{index:03}.png")

        indexed = self.service.index_folder(str(self.root))
        self.assertEqual(indexed.inserted, 103)
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=200,
                external_processing_confirmed=True,
            )

        self.assertEqual(FakeVisionClient.calls, 1)
        self.assertEqual(report["succeeded"], 103)
        self.assertEqual(report["pending_count"], 103)
        self.assertEqual(len(report["proposals"]), 100)
        pending = self.service.pending_auto_tags(limit=100)
        self.assertEqual(pending["pending_count"], 103)
        self.assertEqual(len(pending["proposals"]), 100)
        self.assertTrue(pending["has_more"])

    def test_proposal_exposes_existing_sources_risk_and_identity_boundaries(self):
        self.service.index_folder(str(self.root), tags=["人工标签"])
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )

        proposal = report["proposals"][0]
        source_path = Path(proposal["source_path"])
        self.assertTrue(source_path.is_file())
        self.assertIn("人工标签", proposal["existing_tags"])
        self.assertIn("Cosplay", proposal["low_risk_tags"])
        self.assertEqual(proposal["identity_tags"], [])
        details = {item["tag"]: item for item in proposal["tag_details"]}
        self.assertEqual(details["人工标签"]["source"], "manual")
        folder_tag = proposal["folder_tags"][0]
        self.assertEqual(details[folder_tag]["source"], "folder")
        self.assertEqual(details["Cosplay"]["source"], "model_field")
        self.assertEqual(details["Cosplay"]["risk"], "low")
        self.assertFalse(details["Cosplay"]["identity"])
        self.assertEqual(details["刻晴"]["source"], "accepted_auto")
        self.assertFalse(details["刻晴"]["identity"])
        self.assertFalse(details["刻晴"]["requires_individual_confirmation"])
        source_path.unlink()
        refreshed = self.service.pending_auto_tags(limit=10)
        stale = next(
            item
            for item in refreshed["proposals"]
            if item["doc_id"] == proposal["doc_id"]
        )
        self.assertEqual(stale["source_path"], "")

    def test_pending_filters_latest_entities_fields_and_review_state(self):
        walking = FIELD_SPECS["action"].values[1]
        smiling = FIELD_SPECS["expression"].values[1]
        FakeVisionClient.responses = {
            FLASH_MODEL: [
                annotation_payload(
                    field_values={"action": (walking,), "expression": (smiling,)}
                )
            ]
        }
        self.service.index_folder(str(self.root))
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )

        newest_folder = self.root / "newest"
        newest_folder.mkdir()
        Image.new("RGB", (25, 25), (10, 210, 90)).save(newest_folder / "new.png")
        running = FIELD_SPECS["action"].values[2]
        serious = FIELD_SPECS["expression"].values[3]
        FakeVisionClient.responses = {
            FLASH_MODEL: [
                annotation_payload(
                    field_values={"action": (running,), "expression": (serious,)},
                    requires_review=True,
                    review_reasons=("field:action:conflict",),
                )
            ]
        }
        indexed = self.service.index_folder(str(self.root))
        self.assertEqual(indexed.inserted, 1)
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )

        latest = self.service.pending_auto_tags(
            limit=1,
            filters={
                "latest_index_only": True,
                "character": "刻",
                "work": "神",
                "action": "奔",
                "expression": "严",
                "review_state": "conflict",
            },
        )
        self.assertEqual(latest["pending_count"], 1)
        self.assertEqual(len(latest["proposals"]), 1)
        self.assertFalse(latest["has_more"])
        walking_page = self.service.pending_auto_tags(
            offset=1,
            limit=1,
            filters={"action": "行走", "review_state": "identity"},
        )
        self.assertEqual(walking_page["pending_count"], 0)
        self.assertEqual(len(walking_page["proposals"]), 0)
        self.assertFalse(walking_page["has_more"])
        conflicts = self.service.pending_auto_tags(filters={"review_state": "conflict"})
        self.assertEqual(conflicts["pending_count"], 1)
        self.assertEqual(
            conflicts["proposals"][0]["doc_id"], latest["proposals"][0]["doc_id"]
        )
        low_risk_only = self.service.pending_auto_tags(
            filters={"review_state": "low_risk"}
        )
        self.assertEqual(low_risk_only["pending_count"], 2)

    def test_confirmed_identity_tags_are_auto_accepted_without_confirmation(self):
        self.service.index_folder(str(self.root))
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )
        proposal = report["proposals"][0]
        entry = self.service.state.get(proposal["doc_id"])
        assert entry is not None
        self.assertEqual(entry["accepted_auto_tags"], ["刻晴", "原神"])
        self.assertEqual(proposal["identity_tags"], [])
        accepted = self.service.review_auto_tags(
            [
                {
                    "doc_id": proposal["doc_id"],
                    "action": "accept",
                    "tags": ["Cosplay"],
                }
            ]
        )
        self.assertEqual(accepted["accepted"], 1)

    def test_legacy_pending_identities_reconcile_locally_and_idempotently(self):
        self.service.index_folder(str(self.root))
        entries = self.service.state.list_entries()
        safe = annotation_payload()
        conflict = annotation_payload(
            character=None,
            requires_review=True,
            review_reasons=("entity:character:conflict",),
        )
        conflict["entities"]["character"] = [
            {
                "name": name,
                "state": "conflict",
                "evidence": ["visual"],
                "evidence_text": "",
                "confidence": confidence,
            }
            for name, confidence in (("刻晴", 0.91), ("甘雨", 0.89))
        ]
        for entry, structured, proposed in (
            (entries[0], safe, ["Cosplay", "刻晴", "原神"]),
            (entries[1], conflict, ["Cosplay"]),
        ):
            self.service.state.set_document_annotation(
                doc_id=str(entry["doc_id"]),
                source_sha256=str(entry["sha256"]),
                cache_key="legacy-v2",
                status="pending_review",
                proposed_tags=proposed,
                structured=structured,
                policy={"policy_version": 1},
            )

        preview = self.service.reconcile_pending_identity_tags(dry_run=True)
        self.assertEqual(preview["eligible"], 2)
        self.assertEqual(preview["updated"], 0)
        self.assertEqual(preview["auto_accepted_documents"], 2)
        self.assertEqual(preview["auto_accepted_identity_tags"], 3)
        self.assertEqual(preview["conflict_documents"], 1)
        self.assertEqual(preview["conflict_identity_tags"], 2)
        self.assertEqual(preview["api_request_count"], 0)
        self.assertTrue(
            all(
                not self.service.state.get(str(entry["doc_id"]))["accepted_auto_tags"]
                for entry in entries
            )
        )

        migrated = self.service.reconcile_pending_identity_tags()
        self.assertEqual(migrated["updated"], 2)
        self.assertEqual(migrated["failed"], 0)
        safe_entry = self.service.state.get(str(entries[0]["doc_id"]))
        conflict_entry = self.service.state.get(str(entries[1]["doc_id"]))
        safe_annotation = self.service.state.get_document_annotation(
            str(entries[0]["doc_id"])
        )
        conflict_annotation = self.service.state.get_document_annotation(
            str(entries[1]["doc_id"])
        )
        assert safe_entry is not None
        assert conflict_entry is not None
        assert safe_annotation is not None
        assert conflict_annotation is not None
        self.assertEqual(safe_entry["accepted_auto_tags"], ["刻晴", "原神"])
        self.assertEqual(conflict_entry["accepted_auto_tags"], ["原神"])
        self.assertEqual(safe_annotation["policy"]["policy_version"], 2)
        self.assertTrue(safe_annotation["policy"]["identity_auto_accept_migrated"])
        self.assertEqual(
            set(conflict_annotation["proposed_tags"]),
            {"Cosplay", "刻晴", "甘雨"},
        )
        conflict_page = self.service.pending_auto_tags(
            filters={"review_state": "conflict"}
        )
        self.assertEqual(conflict_page["pending_count"], 1)

        repeated = self.service.reconcile_pending_identity_tags()
        self.assertEqual(repeated["eligible"], 0)
        self.assertEqual(repeated["updated"], 0)
        self.assertEqual(repeated["skipped_current_policy"], 2)

    def test_conflicting_identity_remains_pending_and_accepts_one_choice(self):
        self.service.index_folder(str(self.root))
        FakeVisionClient.responses = {
            FLASH_MODEL: [annotation_payload(character="刻晴", plus_recommended=True)],
            PLUS_MODEL: [annotation_payload(character="甘雨")],
        }
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )

        proposal = report["proposals"][0]
        self.assertEqual(set(proposal["identity_tags"]), {"刻晴", "甘雨"})
        entry = self.service.state.get(proposal["doc_id"])
        assert entry is not None
        self.assertNotIn("刻晴", entry["accepted_auto_tags"])
        self.assertNotIn("甘雨", entry["accepted_auto_tags"])

        rejected = self.service.review_auto_tags(
            [
                {
                    "doc_id": proposal["doc_id"],
                    "action": "accept",
                    "tags": ["Cosplay", "刻晴", "甘雨"],
                    "confirmed_identity_tags": ["刻晴", "甘雨"],
                }
            ]
        )
        self.assertEqual(rejected["accepted"], 0)
        self.assertEqual(rejected["failed"], 1)
        self.assertIn("Only one conflicting identity", rejected["failures"][0]["error"])

        accepted = self.service.review_auto_tags(
            [
                {
                    "doc_id": proposal["doc_id"],
                    "action": "accept",
                    "tags": ["Cosplay", "刻晴"],
                    "confirmed_identity_tags": ["刻晴"],
                }
            ]
        )
        self.assertEqual(accepted["accepted"], 1)

    def test_multiple_confirmed_characters_are_allowed_for_two_people(self):
        self.service.index_folder(str(self.root))
        field_values = {"people_count": ("count_two_people",)}
        FakeVisionClient.responses = {
            FLASH_MODEL: [
                annotation_payload(
                    character="飞鸟马时",
                    field_values=field_values,
                    plus_recommended=True,
                )
            ],
            PLUS_MODEL: [
                annotation_payload(
                    character="调月莉音",
                    field_values=field_values,
                )
            ],
        }
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            report = self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )

        proposal = report["proposals"][0]
        identity_tags = ["飞鸟马时", "调月莉音"]
        self.assertEqual(set(proposal["identity_tags"]), set(identity_tags))

        accepted = self.service.review_auto_tags(
            [
                {
                    "doc_id": proposal["doc_id"],
                    "action": "accept",
                    "tags": [*proposal["low_risk_tags"], *identity_tags],
                    "confirmed_identity_tags": identity_tags,
                }
            ]
        )

        self.assertEqual(accepted["accepted"], 1)
        self.assertEqual(accepted["failed"], 0)
        entry = self.service.state.get(proposal["doc_id"])
        assert entry is not None
        self.assertTrue(set(identity_tags).issubset(entry["accepted_auto_tags"]))

    def test_batch_accepts_one_hundred_low_risk_items_and_undoes_once(self):
        source = self.root / "set-a" / "image.png"
        for index in range(98):
            shutil.copy2(source, self.root / "set-a" / f"batch-{index:03}.png")
        indexed = self.service.index_folder(str(self.root))
        self.assertEqual(indexed.inserted, 100)
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            self.service.auto_tag_images(
                scope="latest_index_run",
                max_images=100,
                external_processing_confirmed=True,
            )
        self.assertEqual(FakeVisionClient.calls, 1)
        proposals = self.service.pending_auto_tags(limit=100)["proposals"]
        proposal_ids = [proposal["doc_id"] for proposal in proposals]
        accepted_by_id = {proposal["doc_id"]: ["Cosplay"] for proposal in proposals}
        calls_before_review = FakeVisionClient.calls
        reviewed = self.service.review_auto_tags_batch(
            proposal_ids=proposal_ids,
            accepted_tags_by_proposal=accepted_by_id,
            exclude_identity_tags=True,
        )
        self.assertEqual(FakeVisionClient.calls, calls_before_review)
        self.assertEqual(reviewed["updated"], 100)
        self.assertEqual(reviewed["accepted_tag_count"], 100)
        self.assertEqual(reviewed["identity_excluded_count"], 0)
        self.assertTrue(reviewed["undo_available"])
        self.assertTrue(self.service.pending_auto_tags(limit=100)["undo_available"])
        for doc_id in proposal_ids:
            entry = self.service.state.get(doc_id)
            annotation = self.service.state.get_document_annotation(doc_id)
            assert entry is not None
            assert annotation is not None
            self.assertEqual(
                entry["accepted_auto_tags"],
                ["刻晴", "原神", "Cosplay"],
            )
            self.assertEqual(annotation["status"], "accepted")
            self.assertNotIn("Cosplay", annotation["proposed_tags"])

        # The undo checkpoint must survive a desktop/backend restart.
        self.service.close()
        del self.service
        gc.collect()
        self.service = ImageVectorService(
            config=ServiceConfig(workspace=self.temporary / "workspace"),
            embedding_client=FakeEmbeddingClient(),
        )
        undone = self.service.undo_latest_auto_tag_review_batch()
        self.assertTrue(undone["undone"])
        self.assertEqual(undone["updated"], 100)
        for doc_id in proposal_ids:
            entry = self.service.state.get(doc_id)
            annotation = self.service.state.get_document_annotation(doc_id)
            assert entry is not None
            assert annotation is not None
            self.assertEqual(entry["accepted_auto_tags"], ["刻晴", "原神"])
            self.assertIn("Cosplay", annotation["proposed_tags"])
        repeated = self.service.undo_latest_auto_tag_review_batch()
        self.assertFalse(repeated["undone"])
        self.assertTrue(repeated["already_undone"])
        self.assertEqual(repeated["batch_id"], reviewed["batch_id"])
        self.assertFalse(self.service.pending_auto_tags(limit=100)["undo_available"])

    def test_batch_failure_restores_repository_and_state(self):
        self.service.index_folder(str(self.root))
        with patch(
            "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
            FakeVisionClient,
        ):
            self.service.auto_tag_images(
                scope="latest_index_run",
                external_processing_confirmed=True,
            )
        proposals = self.service.pending_auto_tags(limit=10)["proposals"]
        original_upsert = self.service.repository.upsert_records
        call_count = 0

        def fail_second_upsert(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return [], {proposals[1]["doc_id"]: "synthetic batch failure"}
            return original_upsert(*args, **kwargs)

        with (
            patch.object(
                self.service.repository,
                "upsert_records",
                side_effect=fail_second_upsert,
            ),
            self.assertRaisesRegex(
                AutoTaggingRequestError, "failed and was rolled back"
            ),
        ):
            self.service.review_auto_tags_batch(
                proposal_ids=[item["doc_id"] for item in proposals],
                accepted_tags_by_proposal={
                    item["doc_id"]: ["Cosplay"] for item in proposals
                },
            )
        for proposal in proposals:
            entry = self.service.state.get(proposal["doc_id"])
            annotation = self.service.state.get_document_annotation(proposal["doc_id"])
            assert entry is not None
            assert annotation is not None
            self.assertEqual(entry["accepted_auto_tags"], ["刻晴", "原神"])
            self.assertEqual(annotation["status"], "pending_review")
            self.assertIn("Cosplay", annotation["proposed_tags"])
        latest = self.service.state.latest_auto_tag_review_batch()
        assert latest is not None
        self.assertEqual(latest["status"], "rolled_back")

    def test_annotation_cache_is_shared_across_collections(self):
        self.service.close()
        shared_results = self.temporary / "shared-results"
        roots = []
        services = []
        for name in ("library-a", "library-b"):
            root = self.temporary / name
            root.mkdir()
            Image.new("RGB", (20, 20), (30, 80, 160)).save(root / "same.png")
            roots.append(root)
            services.append(
                ImageVectorService(
                    config=ServiceConfig(
                        workspace=self.temporary / f"{name}-workspace",
                        results_directory=shared_results,
                    ),
                    embedding_client=FakeEmbeddingClient(),
                )
            )
        try:
            for service, root in zip(services, roots, strict=True):
                service.index_folder(str(root))
            with patch(
                "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
                FakeVisionClient,
            ):
                first = services[0].auto_tag_images(
                    scope="latest_index_run",
                    external_processing_confirmed=True,
                )
                second = services[1].auto_tag_images(
                    scope="latest_index_run",
                    external_processing_confirmed=True,
                )
            self.assertEqual(FakeVisionClient.calls, 1)
            self.assertEqual(first["cached"], 0)
            self.assertEqual(second["cached"], 1)
        finally:
            for service in services:
                service.close()

    def test_concurrent_collections_single_flight_one_visual_request(self):
        self.service.close()
        shared_results = self.temporary / "single-flight-results"
        shared_cache = SharedAutoTagCache(shared_results / "auto_tag_cache.sqlite3")
        shared_cache.close()

        first_root = self.temporary / "single-flight-library-a"
        second_root = self.temporary / "single-flight-library-b"
        first_root.mkdir()
        second_root.mkdir()
        Image.new("RGB", (20, 20), (30, 80, 160)).save(first_root / "same.png")
        shutil.copy2(first_root / "same.png", second_root / "same.png")

        indexed = threading.Barrier(2)
        follower_waiting = threading.Event()
        BlockingVisionClient.reset()
        BlockingVisionClient.calls = 0
        original_wait_for_result = SharedAutoTagCache.wait_for_result

        def observed_wait_for_result(cache, flight, **kwargs):
            follower_waiting.set()
            return original_wait_for_result(cache, flight, **kwargs)

        def run_collection(index: int, root: Path) -> dict[str, Any]:
            service = ImageVectorService(
                config=ServiceConfig(
                    workspace=self.temporary / f"single-flight-workspace-{index}",
                    results_directory=shared_results,
                ),
                embedding_client=FakeEmbeddingClient(),
            )
            try:
                service.index_folder(str(root))
                indexed.wait(timeout=10)
                return service.auto_tag_images(
                    scope="latest_index_run",
                    external_processing_confirmed=True,
                )
            finally:
                service.close()

        try:
            with (
                patch(
                    "image_vector_service.annotation_service."
                    "DashScopeVisionTaggingClient",
                    BlockingVisionClient,
                ),
                patch.object(
                    SharedAutoTagCache,
                    "wait_for_result",
                    new=observed_wait_for_result,
                ),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                futures = [
                    executor.submit(run_collection, 0, first_root),
                    executor.submit(run_collection, 1, second_root),
                ]
                self.assertTrue(BlockingVisionClient.started.wait(timeout=10))
                self.assertTrue(follower_waiting.wait(timeout=10))
                BlockingVisionClient.release.set()
                reports = [future.result(timeout=20) for future in futures]
        finally:
            BlockingVisionClient.release.set()

        self.assertEqual(BlockingVisionClient.calls, 1)
        self.assertEqual(sorted(report["cached"] for report in reports), [0, 1])
        self.assertEqual(sum(report["api_request_count"] for report in reports), 1)

    def test_shared_cache_revalidates_collection_local_entity_evidence(self):
        self.service.close()
        shared_results = self.temporary / "context-results"
        first_root = self.temporary / "context-library-a"
        second_root = self.temporary / "context-library-b"
        first_folder = first_root / "CoserA-CharacterA-WorkA"
        second_folder = second_root / "Other"
        first_folder.mkdir(parents=True)
        second_folder.mkdir(parents=True)
        Image.new("RGB", (20, 20), (30, 80, 160)).save(first_folder / "same.png")
        Image.new("RGB", (20, 20), (30, 80, 160)).save(second_folder / "same.png")
        services = [
            ImageVectorService(
                config=ServiceConfig(
                    workspace=self.temporary / f"context-workspace-{index}",
                    results_directory=shared_results,
                ),
                embedding_client=FakeEmbeddingClient(),
            )
            for index in range(2)
        ]
        FakeVisionClient.responses = {
            FLASH_MODEL: [
                annotation_payload(
                    character="CharacterA",
                    character_evidence=("folder_name",),
                    work="WorkA",
                    work_evidence=("folder_name",),
                    extra_entities={
                        "real_person": [
                            {
                                "name": "Alice",
                                "state": "confirmed",
                                "evidence": ["manual_tag"],
                                "evidence_text": "",
                                "confidence": 0.99,
                            }
                        ],
                        "cosplayer": [
                            {
                                "name": "CoserA",
                                "state": "confirmed",
                                "evidence": ["folder_name"],
                                "evidence_text": "",
                                "confidence": 0.99,
                            }
                        ],
                    },
                )
            ]
        }
        try:
            services[0].index_folder(str(first_root), tags=["Alice"])
            services[1].index_folder(str(second_root), tags=["Bob"])
            with patch(
                "image_vector_service.annotation_service.DashScopeVisionTaggingClient",
                FakeVisionClient,
            ):
                first = services[0].auto_tag_images(
                    scope="latest_index_run",
                    external_processing_confirmed=True,
                )
                second = services[1].auto_tag_images(
                    scope="latest_index_run",
                    external_processing_confirmed=True,
                )
                # Reusing the first Collection again proves the second local
                # downgrade did not mutate the shared cached response.
                first_again = services[0].auto_tag_images(
                    scope="all",
                    external_processing_confirmed=True,
                )

            self.assertEqual(FakeVisionClient.calls, 1)
            self.assertEqual(first["api_request_count"], 1)
            self.assertEqual(second["api_request_count"], 0)
            self.assertEqual(first_again["api_request_count"], 0)
            self.assertEqual(second["cached"], 1)
            self.assertEqual(first_again["cached"], 1)

            first_proposal = first["proposals"][0]
            second_proposal = second["proposals"][0]
            restored_proposal = first_again["proposals"][0]
            for entity_type in ("real_person", "cosplayer", "character", "work"):
                self.assertEqual(
                    first_proposal["entities"][entity_type][0]["state"],
                    "confirmed",
                )
                self.assertEqual(
                    restored_proposal["entities"][entity_type][0]["state"],
                    "confirmed",
                )

            for entity_type in ("real_person", "cosplayer"):
                entity = second_proposal["entities"][entity_type][0]
                self.assertEqual(entity["state"], "unable_to_confirm")
                self.assertIsNone(entity["name"])
            for entity_type in ("character", "work"):
                entity = second_proposal["entities"][entity_type][0]
                self.assertEqual(entity["state"], "suggested")
                self.assertNotIn(entity["name"], second_proposal["proposed_tags"])
            for stale_name in ("Alice", "CoserA", "CharacterA", "WorkA"):
                self.assertNotIn(stale_name, second_proposal["proposed_tags"])
            for entity_type in ("real_person", "cosplayer", "character", "work"):
                self.assertIn(
                    f"entity:{entity_type}:context_mismatch",
                    second_proposal["review_reasons"],
                )

            first_annotation = services[0].state.get_document_annotation(
                first_proposal["doc_id"]
            )
            second_annotation = services[1].state.get_document_annotation(
                second_proposal["doc_id"]
            )
            assert first_annotation is not None
            assert second_annotation is not None
            self.assertEqual(
                first_annotation["cache_key"],
                second_annotation["cache_key"],
            )
        finally:
            for service in services:
                service.close()


if __name__ == "__main__":
    unittest.main()
