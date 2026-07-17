from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from tests.search_quality import capture, evaluate
from tests.search_quality import split as dataset_split


def _item(
    case_id: str,
    mode: str,
    query: dict[str, str],
    *,
    annotation_status: str = "human_verified",
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    is_human_verified = annotation_status == "human_verified"
    return {
        "id": case_id,
        "query_type": mode,
        "mode": mode,
        "query": query,
        "library_scope": scope or {"mode": "single", "library_ids": ["library-a"]},
        "relevant_images": (
            []
            if annotation_status == "pending"
            else [{"image_id": "library-a:images/match.jpg"}]
        ),
        "annotation": {
            "status": annotation_status,
            "annotator": "capture-test" if is_human_verified else None,
            "annotated_at": ("2026-07-13T12:00:00Z" if is_human_verified else None),
            "notes": "test",
        },
    }


def _write_image(path: Path, size: tuple[int, int] = (4, 4)) -> None:
    Image.new("RGB", size, (40, 80, 120)).save(path)


class FakeBackend:
    def __init__(self, staging_root: Path) -> None:
        self.request_count = 0
        self.staging_root = staging_root
        self.params: list[dict[str, Any]] = []
        self.job_modes: dict[str, str] = {}

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.request_count += 1
        if (method, path) == ("GET", "/health"):
            return {"service_ready": True, "protocol_version": 2}
        if (method, path) == ("POST", "/v1/jobs"):
            assert payload is not None
            params = payload["params"]
            self.params.append(params)
            if "image" in params:
                staged_name = Path(str(params["image"])).name
                if not (self.staging_root / staged_name).is_file():
                    raise AssertionError("query image was not staged during submit")
            mode = (
                "combined"
                if "image" in params and "text" in params
                else "image"
                if "image" in params
                else "text"
            )
            job_id = f"{len(self.params):032x}"
            self.job_modes[job_id] = mode
            return {"job": {"id": job_id, "status": "queued"}}
        if method == "GET" and path.startswith("/v1/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            mode = self.job_modes[job_id]
            rank_source = "fused" if mode == "combined" else mode
            request_ids = ["request-1", "request-2"] if mode == "combined" else []
            return {
                "job": {
                    "id": job_id,
                    "status": "succeeded",
                    "result": {
                        "status": "ok",
                        "candidate_count": 4,
                        "filtered_count": 3,
                        "latency_ms": 12.5,
                        "ranking_mode": (
                            "confidence_v2" if mode == "combined" else "confidence"
                        ),
                        "search_quality": {"fusion": {"mode": "confidence_v2"}},
                        "request_ids": request_ids,
                        "library_ids": ["library-a"],
                        "results": [
                            {
                                "rank": 1,
                                "library_id": "library-a",
                                "relative_path": "images\\match.jpg",
                                "sha256": "a" * 64,
                                "raw_score": 0.2,
                                "normalized_score": 0.9,
                                "confidence": 0.85,
                                "match_state": "high",
                                "rank_source": rank_source,
                                "image_confidence": (
                                    0.8 if mode == "combined" else None
                                ),
                                "text_confidence": (
                                    0.9 if mode == "combined" else None
                                ),
                                "image_rank": 2 if mode == "combined" else None,
                                "text_rank": 1 if mode == "combined" else None,
                                "rank_agreement": (
                                    0.9048 if mode == "combined" else None
                                ),
                            }
                        ],
                    },
                }
            }
        raise AssertionError(f"unexpected request: {method} {path}")


class SearchQualityCaptureTest(unittest.TestCase):
    def test_capture_preserves_frozen_dataset_split_role(self):
        manifest_fingerprint = "a" * 64
        for role in dataset_split.ROLES:
            with self.subTest(role=role):
                dataset = {
                    "split": {
                        "kind": dataset_split.SPLIT_KIND,
                        "role": role,
                        "manifest_fingerprint": manifest_fingerprint,
                        "manifest_fingerprint_algorithm": (
                            dataset_split.SPLIT_FINGERPRINT_ALGORITHM
                        ),
                    }
                }
                self.assertEqual(
                    capture._capture_split_metadata(dataset),
                    dataset["split"],
                )

        with self.assertRaisesRegex(ValueError, "bound to a valid manifest"):
            capture._capture_split_metadata(
                {
                    "split": {
                        "kind": dataset_split.SPLIT_KIND,
                        "role": "validation",
                        "manifest_fingerprint": "not-a-digest",
                        "manifest_fingerprint_algorithm": (
                            dataset_split.SPLIT_FINGERPRINT_ALGORITHM
                        ),
                    }
                }
            )

    def test_cli_uses_safe_default_result_and_candidate_budgets(self):
        args = capture._parser().parse_args(
            [
                "--dataset",
                "dataset.local.json",
                "--output",
                "run.local.json",
                "--base-url",
                "http://127.0.0.1:8765",
            ]
        )
        self.assertEqual(args.top_k, 10)
        self.assertEqual(args.candidate_k, 50)

    def test_capture_maps_scopes_stages_images_and_writes_detailed_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_directory = root / "dataset"
            dataset_directory.mkdir()
            source_root = root / "source"
            source_root.mkdir()
            staging = root / "staging"
            staging.mkdir()
            image = source_root / "query.png"
            _write_image(image)
            dataset = {
                "schema_version": 1,
                "name": "human-capture-test",
                "items": [
                    _item(
                        "text-case",
                        "text",
                        {"text": "red sunset"},
                        scope={"mode": "all_enabled", "library_ids": []},
                    ),
                    _item("image-case", "image", {"image": "query.png"}),
                    _item(
                        "combined-case",
                        "combined",
                        {"text": "red sunset", "image": "query.png"},
                        scope={
                            "mode": "selected",
                            "library_ids": ["library-a", "library-b"],
                        },
                    ),
                ],
            }
            backend = FakeBackend(staging)
            run = capture.capture_dataset(
                dataset,
                dataset_directory=dataset_directory,
                query_source_roots=(source_root,),
                client=backend,
                stager=capture.QueryImageStager(staging, "/data/query"),
                top_k=10,
                candidate_k=30,
                poll_interval=0.001,
                job_timeout=1,
                run_name="captured-run",
            )

            evaluate.validate_run(run)
            self.assertTrue(run["baseline_eligible"])
            self.assertFalse(run["draft"])
            self.assertEqual(run["dataset"]["name"], dataset["name"])
            self.assertEqual(
                run["dataset"]["fingerprint"],
                evaluate.query_corpus_fingerprint(dataset),
            )
            self.assertEqual(
                run["dataset"]["fingerprint_algorithm"],
                evaluate.QUERY_CORPUS_FINGERPRINT_ALGORITHM,
            )
            self.assertEqual(run["dataset"]["case_count"], 3)
            self.assertEqual(run["score_fields"]["combined"], "confidence")
            self.assertEqual(backend.params[0].get("library_ids"), None)
            self.assertEqual(backend.params[1]["library_ids"], ["library-a"])
            self.assertEqual(
                backend.params[2]["library_ids"], ["library-a", "library-b"]
            )
            self.assertTrue(
                all(params["candidate_k"] == 30 for params in backend.params)
            )
            self.assertEqual(list(staging.iterdir()), [])

            text_case, image_case, combined_case = run["cases"]
            self.assertEqual(text_case["results"][0]["score"], 0.2)
            self.assertEqual(
                image_case["results"][0]["image_id"], "library-a:images/match.jpg"
            )
            self.assertEqual(image_case["results"][0]["sha256"], "a" * 64)
            self.assertEqual(combined_case["results"][0]["score"], 0.85)
            self.assertEqual(combined_case["api_requests"], 2)
            self.assertEqual(combined_case["backend_requests"], 2)
            self.assertEqual(combined_case["status"], "ok")
            self.assertEqual(combined_case["ranking_mode"], "confidence_v2")
            self.assertEqual(
                combined_case["search_quality"]["fusion"]["mode"],
                "confidence_v2",
            )
            combined_hit = combined_case["results"][0]
            self.assertEqual(combined_hit["image_confidence"], 0.8)
            self.assertEqual(combined_hit["text_confidence"], 0.9)
            self.assertEqual(combined_hit["image_rank"], 2)
            self.assertEqual(combined_hit["text_rank"], 1)

    def test_capture_rejects_invalid_or_duplicate_content_hashes(self):
        base_hit = {
            "rank": 1,
            "library_id": "library-a",
            "relative_path": "images/one.jpg",
            "raw_score": 0.2,
            "normalized_score": 0.9,
            "confidence": 0.9,
            "match_state": "high",
            "rank_source": "text",
        }
        with self.assertRaisesRegex(capture.CaptureError, "64 hexadecimal"):
            capture._capture_results(
                "text",
                {"results": [{**base_hit, "sha256": "not-a-sha256"}]},
            )

        digest = hashlib.sha256(b"same content").hexdigest()
        duplicate = {
            **base_hit,
            "rank": 2,
            "library_id": "library-b",
            "relative_path": "copies/two.jpg",
            "sha256": digest,
        }
        with self.assertRaisesRegex(capture.CaptureError, "duplicate global"):
            capture._capture_results(
                "text",
                {"results": [{**base_hit, "sha256": digest}, duplicate]},
            )

    def test_pending_annotations_are_rejected_unless_explicitly_draft(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend(Path(temporary))
            dataset = {
                "schema_version": 1,
                "name": "pending-capture-test",
                "items": [
                    _item(
                        "pending-text",
                        "text",
                        {"text": "draft query"},
                        annotation_status="pending",
                    )
                ],
            }
            with self.assertRaisesRegex(ValueError, "allow-pending-draft"):
                capture.capture_dataset(
                    dataset,
                    dataset_directory=Path(temporary),
                    client=backend,
                    stager=None,
                )
            self.assertEqual(backend.request_count, 0)

            run = capture.capture_dataset(
                dataset,
                dataset_directory=Path(temporary),
                client=backend,
                stager=None,
                allow_pending_draft=True,
                poll_interval=0.001,
                job_timeout=1,
            )
            self.assertTrue(run["draft"])
            self.assertFalse(run["baseline_eligible"])
            self.assertEqual(run["capture"]["pending_case_count"], 1)
            self.assertEqual(backend.params[0]["top_k"], 10)
            self.assertEqual(backend.params[0]["candidate_k"], 50)
            self.assertEqual(run["capture"]["top_k"], 10)
            self.assertEqual(run["capture"]["candidate_k"], 50)

    def test_api_key_environment_variables_are_never_accepted_as_backend_token(self):
        secret = "api-key-must-not-appear"
        with (
            patch.dict(os.environ, {"DASHSCOPE_API_KEY": secret}),
            self.assertRaises(ValueError) as raised,
        ):
            capture._read_backend_token("DASHSCOPE_API_KEY", None)
        self.assertNotIn(secret, str(raised.exception))

    def test_relative_query_images_cannot_escape_or_be_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_directory = root / "dataset"
            dataset_directory.mkdir()
            source_root = root / "source"
            source_root.mkdir()
            outside = root / "outside.png"
            _write_image(outside)

            escaping = _item(
                "image-case",
                "image",
                {"image": "../outside.png"},
            )
            with self.assertRaisesRegex(capture.CaptureError, "escaped"):
                capture._query_image_path(escaping, dataset_directory, ())

            _write_image(dataset_directory / "query.png")
            _write_image(source_root / "query.png")
            ambiguous = _item("image-case", "image", {"image": "query.png"})
            with self.assertRaisesRegex(capture.CaptureError, "ambiguous"):
                capture._query_image_path(
                    ambiguous,
                    dataset_directory,
                    (source_root,),
                )

    def test_absolute_query_images_remain_supported_with_content_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset_directory = root / "dataset"
            dataset_directory.mkdir()
            staging = root / "staging"
            staging.mkdir()
            image = root / "absolute.png"
            _write_image(image)
            item = _item("image-case", "image", {"image": str(image)})

            resolved = capture._query_image_path(item, dataset_directory, ())
            self.assertEqual(resolved, image)
            stager = capture.QueryImageStager(staging, "/data/query")
            with stager.stage(resolved) as backend_path:
                staged = staging / Path(backend_path).name
                self.assertTrue(staged.is_file())
                if os.name != "nt":
                    self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
            self.assertEqual(list(staging.iterdir()), [])

            wrong_extension = root / "image.txt"
            Image.new("RGB", (4, 4)).save(wrong_extension, format="PNG")
            with (
                self.assertRaisesRegex(capture.CaptureError, "extension"),
                stager.stage(wrong_extension),
            ):
                pass

            corrupt = root / "corrupt.jpg"
            corrupt.write_bytes(b"not an image")
            with (
                self.assertRaisesRegex(capture.CaptureError, "Pillow"),
                stager.stage(corrupt),
            ):
                pass

            too_many_pixels = root / "pixels.png"
            _write_image(too_many_pixels, (2, 2))
            with (
                patch.object(capture, "MAX_QUERY_IMAGE_PIXELS", 3),
                self.assertRaisesRegex(capture.CaptureError, "pixel limit"),
                stager.stage(too_many_pixels),
            ):
                pass

            with (
                patch.object(
                    capture,
                    "MAX_QUERY_IMAGE_BYTES",
                    image.stat().st_size - 1,
                ),
                self.assertRaisesRegex(capture.CaptureError, "size limit"),
                stager.stage(image),
            ):
                pass

    def test_staging_cleanup_failure_is_diagnostic_without_path_disclosure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "staging"
            staging.mkdir()
            image = root / "query.png"
            _write_image(image)
            stager = capture.QueryImageStager(staging, "/data/query")
            error = io.StringIO()
            staged: Path | None = None
            with (
                redirect_stderr(error),
                patch.object(
                    Path,
                    "unlink",
                    autospec=True,
                    side_effect=OSError("sensitive cleanup detail"),
                ),
                stager.stage(image) as backend_path,
            ):
                staged = staging / Path(backend_path).name

            self.assertIsNotNone(staged)
            assert staged is not None
            message = error.getvalue()
            self.assertIn("could not remove", message)
            self.assertNotIn(str(staged), message)
            self.assertNotIn("sensitive cleanup detail", message)
            staged.unlink()

    def test_human_verified_capture_requires_review_metadata_before_requests(self):
        invalid_metadata = (
            ("annotator", None, "needs annotator"),
            ("annotated_at", None, "needs annotated_at"),
            ("annotated_at", "not-a-date", "ISO-8601"),
        )
        for field, value, message in invalid_metadata:
            with (
                self.subTest(field=field, value=value),
                tempfile.TemporaryDirectory() as temporary,
            ):
                dataset: dict[str, Any] = {
                    "schema_version": 1,
                    "name": "invalid-review-metadata",
                    "items": [_item("text-case", "text", {"text": "sunset"})],
                }
                dataset["items"][0]["annotation"][field] = value
                backend = FakeBackend(Path(temporary))
                with self.assertRaisesRegex(ValueError, message):
                    capture.capture_dataset(
                        dataset,
                        dataset_directory=Path(temporary),
                        client=backend,
                        stager=None,
                    )
                self.assertEqual(backend.request_count, 0)


class BackendClientWireTest(unittest.TestCase):
    def test_plain_http_is_limited_to_loopback_hosts(self):
        for base_url in (
            "http://localhost:8765",
            "http://127.0.0.1:8765",
            "http://[::1]:8765",
        ):
            with self.subTest(base_url=base_url):
                capture.BackendClient(base_url, "backend-token")

        for base_url in (
            "http://example.com:8765",
            "http://192.0.2.1:8765",
            "http://localhost.example.com:8765",
        ):
            with (
                self.subTest(base_url=base_url),
                self.assertRaisesRegex(ValueError, "loopback"),
            ):
                capture.BackendClient(base_url, "backend-token")

        capture.BackendClient("https://example.com:8765", "backend-token")

    def test_json_request_has_bearer_auth_and_content_length(self):
        observed: dict[str, Any] = {}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                observed["authorization"] = self.headers.get("Authorization")
                observed["content_length"] = self.headers.get("Content-Length")
                length = int(observed["content_length"])
                observed["payload"] = json.loads(self.rfile.read(length))
                body = json.dumps(
                    {"job": {"id": "a" * 32, "status": "queued"}}
                ).encode()
                self.send_response(202)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = capture.BackendClient(
                f"http://127.0.0.1:{server.server_address[1]}",
                "backend-token",
            )
            response = client.request_json(
                "POST",
                "/v1/jobs",
                {"command": "search", "params": {"text": "sunset"}},
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(response["job"]["status"], "queued")
        self.assertEqual(observed["authorization"], "Bearer backend-token")
        self.assertGreater(int(observed["content_length"]), 0)
        self.assertEqual(observed["payload"]["command"], "search")


if __name__ == "__main__":
    unittest.main()
