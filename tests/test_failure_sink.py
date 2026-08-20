from __future__ import annotations

import errno
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from image_vector_service.failure_sink import FailureSink


class FailureSinkTest(unittest.TestCase):
    def setUp(self) -> None:
        # GitHub's Windows runners can return an 8.3 TEMP path while pathlib
        # resolves files beneath it to the long form.  Keep one canonical root so
        # path assertions and copy-failure injection test filesystem identity,
        # not the spelling chosen by Windows.
        self.temporary = Path(tempfile.mkdtemp(prefix="zvec_failure_sink_")).resolve()
        self.root = self.temporary / "library"
        self.results = self.temporary / "results"
        self.root.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.temporary, ignore_errors=True)

    def _source(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        return path

    @staticmethod
    def _manifest_entries(path: str) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in Path(path).read_text(encoding="utf-8").splitlines()
        ]

    def test_item_failure_is_copied_and_jsonl_remains_auditable(self) -> None:
        source = self._source("broken.jpg", b"invalid image payload")
        expected_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        sink = FailureSink(self.results, "index/job:001", self.root)

        capture = sink.capture(
            path=source,
            error="cannot decode image",
            kind="item",
            stage="scan",
            metadata={"root_id": "root-001", "attempt": 1},
        )

        blob = Path(capture.failure.quarantined_path)
        self.assertTrue(capture.copied)
        self.assertTrue(blob.is_file())
        self.assertEqual(blob.read_bytes(), source.read_bytes())
        self.assertTrue(source.is_file(), "quarantine must copy, never move the source")
        self.assertEqual(capture.failure.sha256, expected_digest)
        self.assertEqual(blob.parent.name, expected_digest[:2])
        self.assertEqual(blob.name, f"{expected_digest}.jpg")
        self.assertEqual(capture.manifest_path, sink.manifest_path)

        entries = self._manifest_entries(capture.manifest_path)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["schema_version"], 1)
        self.assertEqual(entry["job_id"], "indexjob001")
        self.assertEqual(entry["root_path"], str(self.root.resolve()))
        self.assertEqual(entry["source_path"], str(source))
        self.assertEqual(entry["stage"], "scan")
        self.assertEqual(entry["kind"], "item")
        self.assertEqual(entry["sha256"], expected_digest)
        self.assertEqual(entry["blob_path"], str(blob))
        self.assertEqual(entry["metadata"], {"root_id": "root-001", "attempt": 1})
        self.assertTrue(entry["recorded_at"])

    def test_same_sha_is_deduplicated_across_jobs_but_each_event_is_logged(
        self,
    ) -> None:
        content = b"identical corrupt image"
        first_source = self._source("first.jpg", content)
        second_source = self._source("second.png", content)
        first_sink = FailureSink(self.results, "job-a", self.root)
        second_sink = FailureSink(self.results, "job-b", self.root)

        first = first_sink.capture(
            path=first_source,
            error="decode failed",
            kind="item",
            stage="scan",
        )
        second = second_sink.capture(
            path=second_source,
            error="embedding rejected the image",
            kind="item",
            stage="embedding",
        )

        self.assertTrue(first.copied)
        self.assertFalse(second.copied)
        self.assertEqual(
            first.failure.quarantined_path,
            second.failure.quarantined_path,
        )
        blobs = [
            path
            for path in (self.results / "failed-images" / "blobs").rglob("*")
            if path.is_file()
        ]
        self.assertEqual(blobs, [Path(first.failure.quarantined_path)])
        self.assertEqual(
            self._manifest_entries(first.manifest_path)[0]["source_path"],
            str(first_source),
        )
        self.assertEqual(
            self._manifest_entries(second.manifest_path)[0]["source_path"],
            str(second_source),
        )

    def test_retryable_and_systemic_failures_log_without_copying_healthy_files(
        self,
    ) -> None:
        retryable_source = self._source("retryable.png", b"healthy image one")
        systemic_source = self._source("systemic.png", b"healthy image two")
        sink = FailureSink(self.results, "provider-outage", self.root)

        retryable = sink.capture(
            path=retryable_source,
            error="HTTP 429",
            kind="retryable",
            stage="auto_tag",
        )
        systemic = sink.capture(
            path=systemic_source,
            error="invalid API credential",
            kind="systemic",
            stage="auto_tag",
        )

        self.assertFalse(retryable.copied)
        self.assertFalse(systemic.copied)
        self.assertEqual(retryable.failure.quarantined_path, "")
        self.assertEqual(systemic.failure.quarantined_path, "")
        blob_root = self.results / "failed-images" / "blobs"
        self.assertFalse(blob_root.exists())
        entries = self._manifest_entries(sink.manifest_path)
        self.assertEqual(
            [entry["kind"] for entry in entries], ["retryable", "systemic"]
        )
        self.assertTrue(all(entry["blob_path"] == "" for entry in entries))
        self.assertTrue(retryable_source.is_file())
        self.assertTrue(systemic_source.is_file())

    def test_copy_failure_is_logged_and_does_not_stop_following_captures(self) -> None:
        blocked = self._source("blocked.jpg", b"first source")
        healthy = self._source("next.jpg", b"second source")
        sink = FailureSink(self.results, "copy-errors", self.root)
        real_copy2 = shutil.copy2

        def selectively_fail_copy(source: object, destination: object) -> object:
            if Path(source) == blocked:
                raise OSError(errno.EACCES, "copy denied")
            return real_copy2(source, destination)

        with patch(
            "image_vector_service.failure_sink.shutil.copy2",
            side_effect=selectively_fail_copy,
        ):
            failed_copy = sink.capture(
                path=blocked,
                error="bad media",
                kind="item",
                stage="scan",
            )
            following = sink.capture(
                path=healthy,
                error="bad media",
                kind="item",
                stage="scan",
            )

        self.assertFalse(failed_copy.copied)
        self.assertIn("copy denied", failed_copy.failure.copy_error)
        self.assertEqual(failed_copy.failure.quarantined_path, "")
        self.assertFalse(failed_copy.needs_attention)
        self.assertTrue(following.copied)
        self.assertTrue(Path(following.failure.quarantined_path).is_file())
        entries = self._manifest_entries(sink.manifest_path)
        self.assertEqual(len(entries), 2)
        self.assertIn("copy denied", str(entries[0]["copy_error"]))
        self.assertEqual(entries[1]["copy_error"], "")
        partials = list((self.results / "failed-images").rglob("*.partial"))
        self.assertEqual(partials, [])

    def test_stale_expected_sha_never_names_new_bytes_as_old_content(self) -> None:
        source = self._source("changed.jpg", b"original broken bytes")
        expected_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        replacement = b"replacement bytes written while quarantine starts"
        replacement_digest = hashlib.sha256(replacement).hexdigest()
        sink = FailureSink(self.results, "source-change", self.root)
        real_copy2 = shutil.copy2

        def change_before_copy(source_path: object, destination: object) -> object:
            Path(source_path).write_bytes(replacement)
            return real_copy2(source_path, destination)

        with patch(
            "image_vector_service.failure_sink.shutil.copy2",
            side_effect=change_before_copy,
        ):
            capture = sink.capture(
                path=source,
                error="decode failed for the scanned content",
                kind="item",
                stage="embedding",
                sha256_hex=expected_digest,
            )

        self.assertFalse(capture.copied)
        self.assertFalse(capture.needs_attention)
        self.assertEqual(capture.failure.sha256, expected_digest)
        self.assertEqual(capture.failure.quarantined_path, "")
        self.assertIn("source changed", capture.failure.copy_error)
        self.assertIn(expected_digest, capture.failure.copy_error)
        self.assertIn(replacement_digest, capture.failure.copy_error)
        blob_files = [
            path
            for path in (self.results / "failed-images" / "blobs").rglob("*")
            if path.is_file()
        ]
        self.assertEqual(blob_files, [])
        entry = self._manifest_entries(capture.manifest_path)[0]
        self.assertEqual(entry["sha256"], expected_digest)
        self.assertEqual(entry["blob_path"], "")
        self.assertIn("source changed", str(entry["copy_error"]))

    def test_concurrent_jobs_publish_one_blob_even_with_different_suffixes(
        self,
    ) -> None:
        content = b"same invalid image content"
        first_source = self._source("first.jpg", content)
        second_source = self._source("second.png", content)
        digest = hashlib.sha256(content).hexdigest()
        first_sink = FailureSink(self.results, "concurrent-a", self.root)
        second_sink = FailureSink(self.results, "concurrent-b", self.root)
        copy_barrier = threading.Barrier(2)
        real_copy2 = shutil.copy2

        def synchronized_copy(source: object, destination: object) -> object:
            result = real_copy2(source, destination)
            copy_barrier.wait(timeout=3)
            return result

        def capture(sink: FailureSink, source: Path):
            return sink.capture(
                path=source,
                error="decode failed",
                kind="item",
                stage="scan",
                sha256_hex=digest,
            )

        with (
            patch(
                "image_vector_service.failure_sink.shutil.copy2",
                side_effect=synchronized_copy,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            captures = list(
                executor.map(
                    lambda pair: capture(*pair),
                    (
                        (first_sink, first_source),
                        (second_sink, second_source),
                    ),
                )
            )

        self.assertEqual(sum(int(item.copied) for item in captures), 1)
        self.assertEqual(
            len({item.failure.quarantined_path for item in captures}),
            1,
        )
        blob_files = [
            path
            for path in (self.results / "failed-images" / "blobs").rglob("*")
            if path.is_file()
        ]
        self.assertEqual(blob_files, [Path(captures[0].failure.quarantined_path)])
        self.assertEqual(hashlib.sha256(blob_files[0].read_bytes()).hexdigest(), digest)
        self.assertEqual(list((self.results / "failed-images").rglob("*.partial")), [])

    def test_corrupt_existing_blob_is_not_silently_reused(self) -> None:
        source = self._source("broken.jpg", b"stable broken bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        first = FailureSink(self.results, "integrity-a", self.root).capture(
            path=source,
            error="decode failed",
            kind="item",
            stage="scan",
            sha256_hex=digest,
        )
        Path(first.failure.quarantined_path).write_bytes(b"tampered blob")

        second = FailureSink(self.results, "integrity-b", self.root).capture(
            path=source,
            error="decode failed again",
            kind="item",
            stage="scan",
            sha256_hex=digest,
        )

        self.assertFalse(second.copied)
        self.assertTrue(second.needs_attention)
        self.assertEqual(second.failure.quarantined_path, "")
        self.assertIn("checksum mismatch", second.failure.copy_error)
        self.assertEqual(list((self.results / "failed-images").rglob("*.partial")), [])


if __name__ == "__main__":
    unittest.main()
