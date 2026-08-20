from __future__ import annotations

import hashlib
import html
import http.client
import io
import json
import re
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from PIL import Image

from image_vector_service.image_scanner import SUPPORTED_EXTENSIONS
from tests.search_quality import review, review_web

FIXTURE_DIR = Path(__file__).parent / "search_quality"


def _pending_annotation() -> dict[str, object]:
    return {
        "status": "pending",
        "annotator": None,
        "annotated_at": None,
        "notes": "AI suggestion is unverified.",
    }


def _write_image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path)


class ReviewWebFiles:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="zvec-review-web-"))
        self.library_root = self.root / "library-a"
        self.disabled_root = self.root / "library-b"
        self.secret_root = self.root / "secret"
        _write_image(self.library_root / "photos" / "suggested.jpg", (210, 30, 30))
        _write_image(self.library_root / "photos" / "run.jpg", (30, 210, 30))
        _write_image(self.library_root / "photos" / "outside-top-k.jpg", (30, 30, 210))
        corrupt = self.library_root / "photos" / "corrupt.jpg"
        corrupt.write_bytes(b"not an image")
        _write_image(self.library_root / "queries" / "reference.png", (140, 80, 190))
        _write_image(self.disabled_root / "disabled.jpg", (80, 80, 80))
        _write_image(self.secret_root / "private.jpg", (1, 2, 3))
        suggested_sha256 = hashlib.sha256(
            (self.library_root / "photos" / "suggested.jpg").read_bytes()
        ).hexdigest()
        query_sha256 = hashlib.sha256(
            (self.library_root / "queries" / "reference.png").read_bytes()
        ).hexdigest()
        run_sha256 = hashlib.sha256(
            (self.library_root / "photos" / "run.jpg").read_bytes()
        ).hexdigest()
        corrupt_sha256 = hashlib.sha256(corrupt.read_bytes()).hexdigest()

        dataset = {
            "schema_version": 1,
            "name": "visual-review-test",
            "items": [
                {
                    "id": "combined-001",
                    "query_type": "combined",
                    "mode": "combined",
                    "query": {
                        "text": "red sunset",
                        "image": "queries/reference.png",
                    },
                    "library_scope": {
                        "mode": "single",
                        "library_ids": ["library-a"],
                    },
                    "relevant_images": [],
                    "suggested_relevant_images": [
                        {
                            "image_id": "library-a:photos/suggested.jpg",
                            "sha256": suggested_sha256,
                        },
                        {
                            "image_id": "library-a:queries/reference.png",
                            "sha256": query_sha256,
                        },
                    ],
                    "annotation": _pending_annotation(),
                },
                {
                    "id": "no-answer-001",
                    "query_type": "no-answer",
                    "mode": "text",
                    "query": {"text": "an absent subject"},
                    "library_scope": {
                        "mode": "all_enabled",
                        "library_ids": [],
                    },
                    "relevant_images": [],
                    "suggested_relevant_images": [],
                    "annotation": _pending_annotation(),
                },
            ],
        }
        run = {
            "schema_version": 1,
            "name": "visual-review-run",
            "score_semantics": {
                "text": "lower_is_better",
                "image": "lower_is_better",
                "combined": "higher_is_better",
            },
            "cases": [
                {
                    "id": "combined-001",
                    "latency_ms": 1.0,
                    "api_requests": 0,
                    "results": [
                        {
                            "image_id": "library-a:queries/reference.png",
                            "sha256": query_sha256,
                            "rank": 1,
                            "score": 0.99,
                            "confidence": 0.99,
                        },
                        {
                            "image_id": "library-a:photos/run.jpg",
                            "sha256": run_sha256,
                            "rank": 2,
                            "score": 0.91,
                            "confidence": 0.91,
                            "match_state": "high",
                            "rank_source": "fused",
                        },
                        {
                            "image_id": "library-a:photos/corrupt.jpg",
                            "sha256": corrupt_sha256,
                            "rank": 3,
                            "score": 0.80,
                            "confidence": 0.80,
                            "match_state": "high",
                            "rank_source": "fused",
                        },
                    ],
                },
                {
                    "id": "no-answer-001",
                    "latency_ms": 1.0,
                    "api_requests": 0,
                    "results": [
                        {
                            "image_id": "library-a:photos/run.jpg",
                            "sha256": run_sha256,
                            "rank": 1,
                            "score": 0.4,
                            "confidence": 0.4,
                        }
                    ],
                },
            ],
        }
        libraries = {
            "schema_version": 2,
            "libraries": [
                {
                    "id": "library-a",
                    "name": "Enabled Library",
                    "image_root": str(self.library_root),
                    "enabled": True,
                },
                {
                    "id": "library-b",
                    "name": "Disabled Library",
                    "image_root": str(self.disabled_root),
                    "enabled": False,
                },
            ],
        }
        self.dataset_path = self._local_json("dataset", dataset)
        self.run_path = self._local_json("run", run)
        self.libraries_path = self._local_json("libraries", libraries)

    @staticmethod
    def _local_json(name: str, value: dict[str, object]) -> Path:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".local.json",
            prefix=f"review-web-{name}-",
            dir=FIXTURE_DIR,
            delete=False,
        ) as handle:
            json.dump(value, handle, ensure_ascii=False)
            return Path(handle.name)

    def read_dataset(self) -> dict[str, object]:
        value = json.loads(self.dataset_path.read_text(encoding="utf-8"))
        assert isinstance(value, dict)
        return value

    def close(self) -> None:
        for path in (self.dataset_path, self.run_path, self.libraries_path):
            path.unlink(missing_ok=True)
            for temporary in FIXTURE_DIR.glob(f".{path.name}.*.tmp"):
                temporary.unlink(missing_ok=True)
        shutil.rmtree(self.root, ignore_errors=True)


class SearchQualityReviewWebTest(unittest.TestCase):
    TOKEN = "test-review-session-token-123456"

    def setUp(self):
        self.files = ReviewWebFiles()
        self.application = review_web.ReviewWebApplication(
            dataset_path=self.files.dataset_path,
            run_path=self.files.run_path,
            libraries_path=self.files.libraries_path,
            session_token=self.TOKEN,
            max_library_images=20,
        )
        self.server = review_web.create_server(self.application, 0)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.files.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        values = {name.lower(): value for name, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, values, payload

    def page(self, item_id: str) -> str:
        path = f"/?{urlencode({'token': self.TOKEN, 'case': item_id})}"
        status, _headers, payload = self.request("GET", path)
        self.assertEqual(status, 200, payload.decode("utf-8", errors="replace"))
        return payload.decode("utf-8")

    @staticmethod
    def hidden(page: str, name: str) -> str:
        match = re.search(
            rf'name="{re.escape(name)}" value="([^"]*)"',
            page,
        )
        if match is None:
            raise AssertionError(f"missing hidden field {name}")
        return html.unescape(match.group(1))

    def post_confirm(
        self, values: list[tuple[str, str]]
    ) -> tuple[int, dict[str, str], bytes]:
        body = urlencode(values).encode("utf-8")
        return self.request(
            "POST",
            f"/confirm?{urlencode({'token': self.TOKEN})}",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def test_page_covers_full_enabled_library_without_auto_selecting_suggestions(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        page = self.page("combined-001")

        self.assertIn("red sunset", page)
        self.assertIn("AI 建议（未验证）", page)
        self.assertIn("图库其他图片", page)
        self.assertIn("library-a:photos/suggested.jpg", page)
        self.assertIn("library-a:photos/run.jpg", page)
        self.assertIn("library-a:photos/outside-top-k.jpg", page)
        self.assertIn("library-a:photos/corrupt.jpg", page)
        self.assertIn("不可验证 / 不可选择", page)
        self.assertIn('class="candidate candidate-unavailable"', page)
        self.assertNotIn("library-b:disabled.jpg", page)
        self.assertNotIn("library-a:queries/reference.png", page)
        self.assertNotIn(" checked", page)
        self.assertIn('form="review-form"', page)
        self.assertNotIn(str(self.files.library_root), page)
        self.assertNotIn(str(self.files.dataset_path), page)

        selections = [
            html.unescape(value)
            for value in re.findall(
                r'name="image_id" value="([^"]+)" form="review-form"', page
            )
        ]
        self.assertEqual(
            selections,
            [
                "library-a:photos/suggested.jpg",
                "library-a:photos/run.jpg",
                "library-a:photos/outside-top-k.jpg",
            ],
        )
        self.assertEqual(len(selections), len(set(selections)))
        self.assertNotIn("library-a:photos/corrupt.jpg", selections)

    def test_http_form_can_verify_a_top_k_omission_but_not_auto_accept_ai(self):
        page = self.page("combined-001")
        revision = self.hidden(page, "dataset_revision")
        selected = "library-a:photos/outside-top-k.jpg"

        status, headers, _payload = self.post_confirm(
            [
                ("csrf_token", self.TOKEN),
                ("dataset_revision", revision),
                ("item_id", "combined-001"),
                ("annotator", "human-reviewer"),
                ("notes", "Inspected every library image."),
                ("image_id", selected),
                ("human_confirmation", "yes"),
            ]
        )

        self.assertEqual(status, 303)
        self.assertIn("saved=combined-001", headers["location"])
        items = self.files.read_dataset()["items"]
        assert isinstance(items, list)
        answerable = items[0]
        no_answer = items[1]
        assert isinstance(answerable, dict) and isinstance(no_answer, dict)
        self.assertEqual(answerable["annotation"]["status"], "human_verified")
        selected_path = self.files.library_root / "photos" / "outside-top-k.jpg"
        self.assertEqual(
            answerable["relevant_images"],
            [
                {
                    "image_id": selected,
                    "sha256": hashlib.sha256(selected_path.read_bytes()).hexdigest(),
                }
            ],
        )
        self.assertEqual(no_answer["annotation"]["status"], "pending")

    def test_query_token_csrf_host_body_limit_and_candidate_whitelist(self):
        status, _headers, _payload = self.request("GET", "/")
        self.assertEqual(status, 403)

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.putrequest(
            "GET", f"/?{urlencode({'token': self.TOKEN})}", skip_host=True
        )
        connection.putheader("Host", "evil.example")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()

        page = self.page("combined-001")
        revision = self.hidden(page, "dataset_revision")
        common = [
            ("dataset_revision", revision),
            ("item_id", "combined-001"),
            ("annotator", "reviewer"),
            ("notes", "checked"),
            ("human_confirmation", "yes"),
        ]
        status, _headers, _payload = self.post_confirm(
            [("csrf_token", "wrong-token"), *common]
        )
        self.assertEqual(status, 403)

        status, _headers, _payload = self.post_confirm(
            [
                ("csrf_token", self.TOKEN),
                *common,
                ("image_id", "library-a:../../secret/private.jpg"),
            ]
        )
        self.assertEqual(status, 400)

        status, _headers, _payload = self.post_confirm(
            [
                ("csrf_token", self.TOKEN),
                *common,
                ("image_id", "library-a:photos/corrupt.jpg"),
            ]
        )
        self.assertEqual(status, 400)

        oversized = urlencode(
            [
                ("csrf_token", self.TOKEN),
                *common,
                ("notes", "x" * review_web.MAX_REQUEST_BODY_BYTES),
            ]
        ).encode("utf-8")
        status, _headers, _payload = self.request(
            "POST",
            f"/confirm?{urlencode({'token': self.TOKEN})}",
            body=oversized,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 413)
        self.assertEqual(
            self.files.read_dataset()["items"][0]["annotation"]["status"],
            "pending",
        )

    def test_stale_dataset_revision_returns_conflict_without_overwrite(self):
        page = self.page("combined-001")
        stale_revision = self.hidden(page, "dataset_revision")
        dataset = review.load_dataset(self.files.dataset_path)
        dataset["items"][0]["annotation"]["notes"] = "CLI changed this file"
        review._atomic_write(self.files.dataset_path, dataset)

        status, _headers, _payload = self.post_confirm(
            [
                ("csrf_token", self.TOKEN),
                ("dataset_revision", stale_revision),
                ("item_id", "combined-001"),
                ("annotator", "browser-reviewer"),
                ("notes", "stale browser"),
                ("image_id", "library-a:photos/run.jpg"),
                ("human_confirmation", "yes"),
            ]
        )

        self.assertEqual(status, 409)
        item = self.files.read_dataset()["items"][0]
        self.assertEqual(item["annotation"]["status"], "pending")
        self.assertEqual(item["annotation"]["notes"], "CLI changed this file")

    def test_verified_preview_that_disappears_before_submit_fails_closed(self):
        page = self.page("combined-001")
        revision = self.hidden(page, "dataset_revision")
        selected = "library-a:photos/run.jpg"
        (self.files.library_root / "photos" / "run.jpg").unlink()

        status, _headers, _payload = self.post_confirm(
            [
                ("csrf_token", self.TOKEN),
                ("dataset_revision", revision),
                ("item_id", "combined-001"),
                ("annotator", "human-reviewer"),
                ("notes", "file vanished after preview"),
                ("image_id", selected),
                ("human_confirmation", "yes"),
            ]
        )

        self.assertEqual(status, 400)
        item = self.files.read_dataset()["items"][0]
        self.assertEqual(item["annotation"]["status"], "pending")

    def test_same_content_as_query_is_rejected_even_under_another_path(self):
        alias = self.files.library_root / "photos" / "query-copy.png"
        shutil.copy2(
            self.files.library_root / "queries" / "reference.png",
            alias,
        )
        dataset = review.load_dataset(self.files.dataset_path)
        dataset["items"][0]["annotation"]["notes"] = "refresh library view"
        review._atomic_write(self.files.dataset_path, dataset)

        page = self.page("combined-001")
        image_id = "library-a:photos/query-copy.png"
        self.assertIn(image_id, page)
        revision = self.hidden(page, "dataset_revision")
        status, _headers, _payload = self.post_confirm(
            [
                ("csrf_token", self.TOKEN),
                ("dataset_revision", revision),
                ("item_id", "combined-001"),
                ("annotator", "human-reviewer"),
                ("notes", "same bytes under another path"),
                ("image_id", image_id),
                ("human_confirmation", "yes"),
            ]
        )

        self.assertEqual(status, 400)
        item = self.files.read_dataset()["items"][0]
        self.assertEqual(item["annotation"]["status"], "pending")

    def test_no_answer_requires_two_explicit_confirmations_and_has_no_selection(self):
        page = self.page("no-answer-001")
        self.assertNotIn('name="image_id"', page)
        revision = self.hidden(page, "dataset_revision")
        common = [
            ("csrf_token", self.TOKEN),
            ("dataset_revision", revision),
            ("item_id", "no-answer-001"),
            ("annotator", "human-reviewer"),
            ("notes", "Inspected the enabled library."),
            ("human_confirmation", "yes"),
        ]
        status, _headers, _payload = self.post_confirm(common)
        self.assertEqual(status, 400)

        status, _headers, _payload = self.post_confirm(
            [*common, ("confirm_no_answer", "yes")]
        )
        self.assertEqual(status, 303)
        item = self.files.read_dataset()["items"][1]
        self.assertEqual(item["annotation"]["status"], "human_verified")
        self.assertEqual(item["relevant_images"], [])

    def test_media_is_opaque_verified_thumbnail_and_arbitrary_paths_are_rejected(self):
        page = self.page("combined-001")
        media_path = re.search(r'src="(/media/[^"]+)"', page)
        self.assertIsNotNone(media_path)
        assert media_path is not None
        status, headers, payload = self.request(
            "GET", html.unescape(media_path.group(1))
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/jpeg")
        self.assertLessEqual(len(payload), review_web.MAX_THUMBNAIL_BYTES)
        with Image.open(io.BytesIO(payload)) as image:
            self.assertLessEqual(image.width, 1600)
            self.assertLessEqual(image.height, 1600)

        status, _headers, response = self.request(
            "GET", f"/media/../../private.jpg?{urlencode({'token': self.TOKEN})}"
        )
        self.assertEqual(status, 404)
        self.assertNotIn(str(self.files.secret_root).encode(), response)

    def test_library_limit_disabled_scope_and_reparse_escape_fail_closed(self):
        with self.assertRaisesRegex(review.ReviewError, "exceeds"):
            review_web.ReviewWebApplication(
                dataset_path=self.files.dataset_path,
                run_path=self.files.run_path,
                libraries_path=self.files.libraries_path,
                session_token=self.TOKEN,
                max_library_images=3,
            )

        dataset = review.load_dataset(self.files.dataset_path)
        dataset["items"][0]["library_scope"] = {
            "mode": "single",
            "library_ids": ["library-b"],
        }
        review._atomic_write(self.files.dataset_path, dataset)
        with self.assertRaisesRegex(review.ReviewError, "disabled library"):
            review_web.ReviewWebApplication(
                dataset_path=self.files.dataset_path,
                run_path=self.files.run_path,
                libraries_path=self.files.libraries_path,
                session_token=self.TOKEN,
                max_library_images=20,
            )

        link = self.files.library_root / "escape.jpg"
        try:
            link.symlink_to(self.files.secret_root / "private.jpg")
        except OSError:
            return
        self.assertIsNone(review_web._path_under(self.files.library_root, "escape.jpg"))

    def test_preview_formats_decode_size_and_pixel_limits_match_the_indexer(self):
        self.assertEqual(
            set(review_web.ALLOWED_IMAGE_TYPES),
            set(SUPPORTED_EXTENSIONS),
        )
        corrupt = self.files.library_root / "photos" / "corrupt.tif"
        corrupt.write_bytes(b"not-an-image")
        self.assertIsNone(review_web._inspect_image(corrupt))

        valid = self.files.library_root / "photos" / "run.jpg"
        with patch.object(review_web, "MAX_MEDIA_BYTES", 1):
            self.assertIsNone(review_web._inspect_image(valid))
        with patch.object(review_web, "MAX_IMAGE_PIXELS", 1):
            self.assertIsNone(review_web._inspect_image(valid))


if __name__ == "__main__":
    unittest.main()
