from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from zvec_lan import (
    InvalidUpload,
    QueryImageNotFound,
    QueryImageStore,
    UploadCancelled,
    UploadStorageError,
)


class _Clock:
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class QueryImageStoreTests(unittest.TestCase):
    def test_streams_many_bounded_chunks_without_a_total_size_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueryImageStore(directory, write_chunk_bytes=64 * 1024)
            self.addCleanup(store.close)
            block = b"z" * (256 * 1024)

            image = store.store(
                (block for _index in range(32)),
                owner_id="android-1",
                display_name=r"C:\Users\secret\large-query.jpg",
                content_type="image/jpeg",
            )

            self.assertEqual(image.size_bytes, 8 * 1024 * 1024)
            self.assertEqual(image.path.stat().st_size, image.size_bytes)
            self.assertEqual(image.display_name, "large-query.jpg")
            self.assertEqual(image.content_type, "image/jpeg")

    def test_completed_upload_is_owner_scoped_and_deletable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueryImageStore(directory)
            image = store.store(
                [b"abc", b"def"],
                owner_id="android-1",
                display_name="query.png",
                content_type="image/png",
            )

            self.assertEqual(
                store.get(image.query_image_id, owner_id="android-1").path.read_bytes(),
                b"abcdef",
            )
            with self.assertRaises(QueryImageNotFound):
                store.get(image.query_image_id, owner_id="android-2")
            store.delete(image.query_image_id, owner_id="android-1")
            self.assertFalse(image.path.exists())
            with self.assertRaises(QueryImageNotFound):
                store.get(image.query_image_id, owner_id="android-1")

    def test_disconnect_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueryImageStore(directory)

            def interrupted() -> object:
                yield b"partial"
                raise ConnectionError("client disconnected")

            with self.assertRaises(UploadCancelled):
                store.store(
                    interrupted(),
                    owner_id="android-1",
                    display_name="query.png",
                    content_type="image/png",
                )

            self.assertEqual(list(Path(directory).glob("zvec-query-*")), [])

    def test_invalid_chunk_type_is_rejected_and_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueryImageStore(directory)

            with self.assertRaises(InvalidUpload):
                store.store(
                    [b"valid", "not-bytes"],
                    owner_id="android-1",
                    display_name="query.png",
                    content_type="image/png",
                )

            self.assertEqual(list(Path(directory).glob("zvec-query-*")), [])

    def test_expiry_and_recovery_cleanup_remove_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale_part = root / "zvec-query-recovered-upload-1.part"
            stale_bin = root / "zvec-query-recovered-upload-2.bin"
            stale_part.write_bytes(b"partial")
            stale_bin.write_bytes(b"complete")
            os.utime(stale_bin, (0, 0))
            clock = _Clock()

            store = QueryImageStore(
                root,
                stale_after_seconds=60,
                clock=clock,
            )

            self.assertFalse(stale_part.exists())
            self.assertFalse(stale_bin.exists())
            image = store.store(
                [b"query"],
                owner_id="android-1",
                display_name="query.jpg",
                content_type="image/jpeg",
            )
            clock.now += 60
            self.assertGreaterEqual(store.cleanup_stale(), 1)
            self.assertFalse(image.path.exists())

    def test_parallel_uploads_publish_independent_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueryImageStore(directory)

            def upload(index: int) -> tuple[str, bytes]:
                content = f"image-{index}".encode()
                image = store.store(
                    [content],
                    owner_id="android-1",
                    display_name=f"{index}.jpg",
                    content_type="image/jpeg",
                )
                return image.query_image_id, image.path.read_bytes()

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(upload, range(16)))

            self.assertEqual(len({query_id for query_id, _body in results}), 16)
            self.assertEqual(
                {body for _query_id, body in results},
                {f"image-{index}".encode() for index in range(16)},
            )
            store.close()
            self.assertEqual(list(Path(directory).glob("*.bin")), [])

    def test_known_lengths_are_reserved_atomically_across_parallel_uploads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_started = threading.Event()
            release_first = threading.Event()
            store = QueryImageStore(
                directory,
                min_free_bytes=10,
                free_space_provider=lambda _path: 20,
            )

            def blocked_body() -> object:
                first_started.set()
                self.assertTrue(release_first.wait(2))
                yield b"123456"

            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(
                    store.store,
                    blocked_body(),
                    owner_id="android-1",
                    display_name="first.jpg",
                    content_type="image/jpeg",
                    expected_size_bytes=6,
                )
                self.assertTrue(first_started.wait(2))
                with self.assertRaises(UploadStorageError):
                    store.store(
                        [b"abcdef"],
                        owner_id="android-2",
                        display_name="second.jpg",
                        content_type="image/jpeg",
                        expected_size_bytes=6,
                    )
                release_first.set()
                image = first.result(timeout=2)

            self.assertEqual(image.path.read_bytes(), b"123456")
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_unknown_length_rechecks_space_and_cleans_partial_on_exhaustion(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            free_values = iter((20, 11))
            store = QueryImageStore(
                directory,
                write_chunk_bytes=2,
                min_free_bytes=10,
                space_check_interval_bytes=4,
                free_space_provider=lambda _path: next(free_values),
            )

            with self.assertRaises(UploadStorageError):
                store.store(
                    [b"ab", b"cd", b"ef"],
                    owner_id="android-1",
                    display_name="query.jpg",
                    content_type="image/jpeg",
                )

            self.assertEqual(list(Path(directory).glob("zvec-query-*")), [])

    def test_failed_upload_releases_reservation_for_following_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = QueryImageStore(
                directory,
                min_free_bytes=10,
                free_space_provider=lambda _path: 16,
            )

            with self.assertRaises(InvalidUpload):
                store.store(
                    [b"short"],
                    owner_id="android-1",
                    display_name="broken.jpg",
                    content_type="image/jpeg",
                    expected_size_bytes=6,
                )
            image = store.store(
                [b"second"],
                owner_id="android-1",
                display_name="second.jpg",
                content_type="image/jpeg",
                expected_size_bytes=6,
            )

            self.assertEqual(image.path.read_bytes(), b"second")
            self.assertEqual(list(Path(directory).glob("*.part")), [])

    def test_close_cancels_active_upload_and_releases_its_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_written = threading.Event()
            continue_body = threading.Event()
            store = QueryImageStore(
                directory,
                write_chunk_bytes=3,
                min_free_bytes=10,
                free_space_provider=lambda _path: 20,
            )

            def body() -> object:
                yield b"abc"
                first_written.set()
                self.assertTrue(continue_body.wait(2))
                yield b"def"

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    store.store,
                    body(),
                    owner_id="android-1",
                    display_name="query.jpg",
                    content_type="image/jpeg",
                    expected_size_bytes=6,
                )
                self.assertTrue(first_written.wait(2))
                store.close()
                replacement = store.store(
                    [b"123456"],
                    owner_id="android-1",
                    display_name="replacement.jpg",
                    content_type="image/jpeg",
                    expected_size_bytes=6,
                )
                continue_body.set()
                with self.assertRaises(UploadCancelled):
                    future.result(timeout=2)

            self.assertEqual(replacement.path.read_bytes(), b"123456")
            self.assertEqual(list(Path(directory).glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
