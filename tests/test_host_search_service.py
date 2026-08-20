from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

from PIL import Image

from zvec_host.search_service import (
    SearchProtocolError,
    SearchRequest,
    SearchService,
    SearchServiceError,
    SearchValidationError,
    SearchWaitTimeout,
    normalize_search_request,
)


class FakeSearchClient:
    def __init__(self) -> None:
        self.submissions: list[tuple[str, dict[str, Any]]] = []
        self.jobs: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.submit_error: Exception | None = None

    def submit_job(
        self, command: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        self.submissions.append((command, dict(params or {})))
        if self.submit_error is not None:
            raise self.submit_error
        return {"id": "job-1", "status": "queued"}

    def get_job(self, job_id: str) -> dict[str, Any]:
        if not self.jobs:
            return {"id": job_id, "status": "running"}
        if len(self.jobs) == 1:
            return dict(self.jobs[0])
        return dict(self.jobs.pop(0))

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        self.cancelled.append(job_id)
        return {"id": job_id, "status": "cancelled"}


class HostSearchRequestValidationTest(unittest.TestCase):
    def test_text_image_and_combined_query_types(self) -> None:
        self.assertEqual(
            normalize_search_request(SearchRequest(text="  海边日落 ")).query_type,
            "text",
        )
        self.assertEqual(
            normalize_search_request(SearchRequest(image_path="query.jpg")).query_type,
            "image",
        )
        combined = normalize_search_request(
            SearchRequest(
                text="角色动作",
                image_path="query.jpg",
                image_weight=7,
                text_weight=3,
            )
        )
        self.assertEqual(combined.query_type, "combined")
        self.assertAlmostEqual(combined.image_weight, 0.7)
        self.assertAlmostEqual(combined.text_weight, 0.3)

    def test_tag_query_is_fuzzy_text_only_mode(self) -> None:
        normalized = normalize_search_request(
            SearchRequest(text=" 神 ", search_mode="tags")
        )
        self.assertEqual(normalized.query_type, "tag")
        self.assertEqual(normalized.text, "神")
        with self.assertRaisesRegex(SearchValidationError, "不能同时提供图片"):
            normalize_search_request(
                SearchRequest(text="原", image_path="a.jpg", search_mode="tags")
            )

    def test_values_are_deduplicated_and_candidate_budget_covers_top_k(self) -> None:
        normalized = normalize_search_request(
            SearchRequest(
                text="cosplay",
                top_k=15,
                candidate_k=50,
                library_ids=("lib-a", "lib-a", "lib-b"),
                tags=(" 原神 ", "原神", "动作"),
            )
        )
        self.assertEqual(normalized.library_ids, ("lib-a", "lib-b"))
        self.assertEqual(normalized.tags, ("原神", "动作"))
        expanded = normalize_search_request(
            SearchRequest(text="x", top_k=15, candidate_k=10)
        )
        self.assertEqual(expanded.candidate_k, 15)
        self.assertEqual(expanded.sort_mode, "confidence")

    def test_result_count_has_no_legacy_500_item_product_cap(self) -> None:
        normalized = normalize_search_request(
            SearchRequest(text="cosplay", top_k=750, candidate_k=750)
        )
        self.assertEqual(normalized.top_k, 750)
        self.assertEqual(normalized.candidate_k, 750)

    def test_empty_and_invalid_weight_requests_are_rejected(self) -> None:
        with self.assertRaisesRegex(SearchValidationError, "请输入"):
            normalize_search_request(SearchRequest(text="  "))
        with self.assertRaisesRegex(SearchValidationError, "不能同时为零"):
            normalize_search_request(
                SearchRequest(
                    text="x",
                    image_path="a.jpg",
                    image_weight=0,
                    text_weight=0,
                )
            )

    def test_invalid_sort_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(SearchValidationError, "sort_mode"):
            normalize_search_request(
                SearchRequest(text="x", sort_mode="unknown")  # type: ignore[arg-type]
            )


class HostSearchServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.query_root = self.root / "query-staging"
        self.client = FakeSearchClient()
        self.service = SearchService(self.client, self.query_root)

    def _image(self, name: str = "查询 图片.JPG") -> Path:
        path = self.root / name
        path.write_bytes(b"not-real-image-but-copy-contract-is-valid")
        return path

    def _output(self) -> Path:
        output = self.root / "results" / "search-1"
        output.mkdir(parents=True)
        (output / "results.json").write_text(
            '{"schema_version":1,"results":[]}', encoding="utf-8"
        )
        return output

    def _oversized_lan_jpeg(self, name: str = "opaque-upload.bin") -> Path:
        path = self.root / name
        exif = Image.Exif()
        exif[274] = 6
        Image.new("RGB", (96, 48), (32, 96, 180)).save(
            path,
            format="JPEG",
            quality=92,
            exif=exif,
        )
        # A legal JPEG may contain trailing application bytes. This creates a
        # source above the injected model threshold without allocating a huge
        # test image or weakening the production byte-unbounded contract.
        with path.open("ab") as stream:
            stream.write(b"trailing-lan-payload" * 4096)
        return path

    def test_submit_stages_image_and_builds_exact_backend_payload(self) -> None:
        source = self._image()
        submission = self.service.submit(
            SearchRequest(
                text=" 雷电将军 ",
                image_path=source,
                library_ids=("lib-a", "lib-b"),
                tags=("原神",),
                top_k=15,
                candidate_k=50,
                image_weight=8,
                text_weight=2,
            )
        )

        self.assertEqual(submission.query_type, "combined")
        self.assertIsNotNone(submission.staged_image)
        assert submission.staged_image is not None
        self.assertEqual(submission.staged_image.parent, self.query_root.resolve())
        self.assertEqual(submission.staged_image.read_bytes(), source.read_bytes())
        command, params = self.client.submissions[0]
        self.assertEqual(command, "search")
        self.assertEqual(params["text"], "雷电将军")
        self.assertEqual(params["image"], str(submission.staged_image))
        self.assertEqual(params["library_ids"], ["lib-a", "lib-b"])
        self.assertEqual(params["tags"], ["原神"])
        self.assertEqual(params["top_k"], 15)
        self.assertEqual(params["candidate_k"], 50)
        self.assertEqual(params["sort_mode"], "confidence")
        self.assertAlmostEqual(params["image_weight"], 0.8)
        self.assertAlmostEqual(params["text_weight"], 0.2)

    def test_submit_failure_removes_private_staging_copy(self) -> None:
        self.client.submit_error = RuntimeError("offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.service.submit(SearchRequest(image_path=self._image()))
        self.assertEqual(list(self.query_root.glob("*")), [])

    def test_optional_size_limit_preserves_streaming_lan_uploads(self) -> None:
        source = self._image()
        limited = SearchService(
            self.client,
            self.query_root / "limited",
            max_query_image_bytes=4,
        )
        with self.assertRaisesRegex(SearchValidationError, "不能超过"):
            limited.submit(SearchRequest(image_path=source))

        unbounded = SearchService(
            self.client,
            self.query_root / "lan",
            max_query_image_bytes=None,
        )
        submission = unbounded.submit(SearchRequest(image_path=source))

        assert submission.staged_image is not None
        self.assertEqual(submission.staged_image.read_bytes(), source.read_bytes())

    def test_lan_oversize_image_is_bounded_without_reading_or_mutating_source(
        self,
    ) -> None:
        source = self._oversized_lan_jpeg()
        digest_before = _sha256_file(source)
        stat_before = source.stat()
        service = SearchService(
            self.client,
            self.query_root / "lan-transcoded",
            max_query_image_bytes=None,
            model_source_image_bytes=4096,
            max_image_data_uri_bytes=4096,
            max_decode_pixels=100_000,
            max_output_dimension=128,
        )

        with patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("source must not be read wholesale"),
        ):
            submission = service.submit(SearchRequest(image_path=source))

        assert submission.staged_image is not None
        staged = submission.staged_image
        self.assertEqual(staged.suffix, ".jpg")
        self.assertLessEqual(staged.stat().st_size, 4096)
        encoded_size = 23 + 4 * ((staged.stat().st_size + 2) // 3)
        self.assertLessEqual(encoded_size, 4096)
        with Image.open(staged) as image:
            self.assertEqual(image.format, "JPEG")
            # EXIF orientation 6 rotates the 96x48 source clockwise.
            self.assertEqual(image.size, (48, 96))
            image.verify()
        self.assertEqual(_sha256_file(source), digest_before)
        stat_after = source.stat()
        self.assertEqual(stat_after.st_size, stat_before.st_size)
        self.assertEqual(stat_after.st_mtime_ns, stat_before.st_mtime_ns)
        self.assertEqual(self.client.submissions[0][1]["image"], str(staged))

    def test_lan_transcode_flattens_transparency_to_existing_white_background(
        self,
    ) -> None:
        source = self.root / "transparent-upload.bin"
        image = Image.new("RGBA", (64, 32), (0, 0, 0, 0))
        image.paste((220, 30, 40, 255), (0, 0, 32, 32))
        image.save(source, format="PNG")
        image.close()
        with source.open("ab") as stream:
            stream.write(b"padding" * 2048)
        service = SearchService(
            self.client,
            self.query_root / "transparent",
            max_query_image_bytes=None,
            model_source_image_bytes=4096,
            max_image_data_uri_bytes=4096,
        )

        submission = service.submit(SearchRequest(image_path=source))

        assert submission.staged_image is not None
        with Image.open(submission.staged_image) as staged:
            self.assertEqual(staged.mode, "RGB")
            red, green, blue = staged.getpixel((56, 16))
            self.assertGreater(red, 240)
            self.assertGreater(green, 240)
            self.assertGreater(blue, 240)

    def test_lan_opaque_small_upload_gets_content_derived_extension(self) -> None:
        source = self.root / "small-upload.bin"
        Image.new("RGB", (16, 16), (20, 40, 60)).save(source, format="PNG")
        service = SearchService(
            self.client,
            self.query_root / "content-suffix",
            max_query_image_bytes=None,
        )

        submission = service.submit(SearchRequest(image_path=source))

        assert submission.staged_image is not None
        self.assertEqual(submission.staged_image.suffix, ".png")
        self.assertEqual(
            _sha256_file(submission.staged_image),
            _sha256_file(source),
        )

    def test_lan_corrupt_or_pixel_dangerous_image_leaves_no_staging_file(
        self,
    ) -> None:
        corrupt = self.root / "corrupt.bin"
        corrupt.write_bytes(b"not-an-image" * 1024)
        corrupt_root = self.query_root / "corrupt"
        corrupt_service = SearchService(
            self.client,
            corrupt_root,
            max_query_image_bytes=None,
            model_source_image_bytes=128,
            max_image_data_uri_bytes=4096,
        )
        with self.assertRaisesRegex(SearchValidationError, "损坏|解码"):
            corrupt_service.submit(SearchRequest(image_path=corrupt))
        self.assertEqual(list(corrupt_root.glob("*")), [])

        dangerous = self.root / "dangerous.bin"
        Image.new("RGB", (32, 32), (10, 20, 30)).save(
            dangerous,
            format="PNG",
        )
        with dangerous.open("ab") as stream:
            stream.write(b"padding" * 1024)
        dangerous_root = self.query_root / "dangerous"
        dangerous_service = SearchService(
            self.client,
            dangerous_root,
            max_query_image_bytes=None,
            model_source_image_bytes=128,
            max_image_data_uri_bytes=4096,
            max_decode_pixels=100,
        )
        with self.assertRaisesRegex(SearchValidationError, "像素"):
            dangerous_service.submit(SearchRequest(image_path=dangerous))
        self.assertEqual(list(dangerous_root.glob("*")), [])

    def test_lan_transcoded_copy_is_removed_when_backend_submit_fails(self) -> None:
        source = self._oversized_lan_jpeg("submit-failure.bin")
        root = self.query_root / "submit-failure"
        service = SearchService(
            self.client,
            root,
            max_query_image_bytes=None,
            model_source_image_bytes=4096,
            max_image_data_uri_bytes=4096,
        )
        self.client.submit_error = RuntimeError("offline")

        with self.assertRaisesRegex(RuntimeError, "offline"):
            service.submit(SearchRequest(image_path=source))

        self.assertEqual(list(root.glob("*")), [])

    def test_lan_transcode_disk_failure_is_safe_and_leaves_no_partial_file(
        self,
    ) -> None:
        source = self._oversized_lan_jpeg("disk-full.bin")
        root = self.query_root / "disk-full"
        service = SearchService(
            self.client,
            root,
            max_query_image_bytes=None,
            model_source_image_bytes=4096,
            max_image_data_uri_bytes=4096,
        )

        with (
            patch.object(Image.Image, "save", side_effect=OSError("disk full")),
            self.assertRaisesRegex(SearchServiceError, "磁盘空间不足"),
        ):
            service.submit(SearchRequest(image_path=source))

        self.assertEqual(list(root.glob("*")), [])

    def test_wait_returns_manifest_reports_progress_and_cleans_staging(self) -> None:
        submission = self.service.submit(SearchRequest(image_path=self._image()))
        output = self._output()
        self.client.jobs = [
            {"id": "job-1", "status": "running", "progress": {"done": 1}},
            {
                "id": "job-1",
                "status": "succeeded",
                "progress": {"done": 2},
                "result": {"output_dir": str(output), "result_count": 0},
                "error": None,
            },
        ]
        updates: list[str] = []

        outcome = self.service.wait(
            submission,
            timeout=1,
            poll_interval=0.001,
            on_progress=lambda job: updates.append(str(job["status"])),
        )

        self.assertTrue(outcome.successful)
        self.assertEqual(outcome.manifest_path, output / "results.json")
        self.assertEqual(updates, ["running", "succeeded"])
        assert submission.staged_image is not None
        self.assertFalse(submission.staged_image.exists())

    def test_wait_forwards_cancellation_and_waits_for_terminal_proof(self) -> None:
        submission = self.service.submit(SearchRequest(image_path=self._image()))
        event = threading.Event()
        event.set()
        self.client.jobs = [
            {"id": "job-1", "status": "running"},
            {"id": "job-1", "status": "cancelled", "error": None},
        ]

        outcome = self.service.wait(
            submission,
            timeout=1,
            poll_interval=0.001,
            cancel_event=event,
        )

        self.assertEqual(self.client.cancelled, ["job-1"])
        self.assertEqual(outcome.status, "cancelled")
        assert submission.staged_image is not None
        self.assertFalse(submission.staged_image.exists())

    def test_timeout_retains_image_for_a_still_running_backend(self) -> None:
        submission = self.service.submit(SearchRequest(image_path=self._image()))
        with self.assertRaises(SearchWaitTimeout):
            self.service.wait(submission, timeout=0.002, poll_interval=0.001)
        assert submission.staged_image is not None
        self.assertTrue(submission.staged_image.exists())

    def test_success_without_manifest_is_a_protocol_error_and_still_cleans(
        self,
    ) -> None:
        submission = self.service.submit(SearchRequest(image_path=self._image()))
        output = self.root / "missing-manifest"
        output.mkdir()
        self.client.jobs = [
            {
                "id": "job-1",
                "status": "succeeded",
                "result": {"output_dir": str(output)},
            }
        ]
        with self.assertRaisesRegex(SearchProtocolError, "results.json"):
            self.service.wait(submission, timeout=1, poll_interval=0.001)
        assert submission.staged_image is not None
        self.assertFalse(submission.staged_image.exists())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
