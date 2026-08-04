"""Tests for the independent ARW selection module (raw_selection).

Covers requirement sections 14.1 (import & persistence) and 14.2 (decode &
cache). ARW decode paths are exercised only for error handling here (real
A7M4 samples are not part of the repo); JPG/PNG decode uses real generated
images via Pillow.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from image_vector_service.raw_selection.cache import DerivedCache
from image_vector_service.raw_selection.creative_look import (
    DEFAULT_LOOK,
    VALID_LOOKS,
    apply_creative_look,
    is_valid_look,
)
from image_vector_service.raw_selection.decoder import (
    decode_preview,
    decode_thumbnail,
)
from image_vector_service.raw_selection.scheduler import DecodeScheduler
from image_vector_service.raw_selection.service import RawSelectionService


def _write_jpg(path: Path, size=(200, 150), color=(255, 0, 0)) -> None:
    Image.new("RGB", size, color=color).save(path, format="JPEG")


def _write_png(path: Path, size=(120, 120)) -> None:
    Image.new("RGBA", size, color=(0, 255, 0, 128)).save(path, format="PNG")


class RawSelectionDBTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.svc = RawSelectionService(Path(self._tmp.name))
        self.addCleanup(self.svc.close)

    def test_project_crud(self) -> None:
        p = self.svc.create_project("Session")
        self.assertEqual(p["name"], "Session")
        self.assertEqual(self.svc.get_project(p["id"])["member_count"], 0)
        self.assertEqual(self.svc.get_project(p["id"])["cover_member_ids"], [])

        renamed = self.svc.rename_project(p["id"], "Renamed")
        self.assertEqual(renamed["name"], "Renamed")

        self.assertTrue(self.svc.delete_project(p["id"]))
        self.assertIsNone(self.svc.get_project(p["id"]))

    def test_project_cover_uses_only_first_imported_member(self) -> None:
        p = self.svc.create_project("Cover")
        folder = Path(self._tmp.name) / "cover"
        folder.mkdir()
        first_path = folder / "z-first.jpg"
        second_path = folder / "a-second.jpg"
        _write_jpg(first_path)
        _write_jpg(second_path)

        self.svc.import_files(p["id"], [first_path, second_path])
        members = self.svc.list_members(p["id"], sort_field="import_order")["members"]
        self.assertEqual(
            [member["file_name"] for member in members],
            ["z-first.jpg", "a-second.jpg"],
        )

        expected_cover = [members[0]["id"]]
        self.assertEqual(
            self.svc.get_project(p["id"])["cover_member_ids"], expected_cover
        )
        self.assertEqual(
            self.svc.list_projects()[0]["cover_member_ids"], expected_cover
        )

    def test_project_name_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.create_project("   ")
        with self.assertRaises(ValueError):
            self.svc.create_project("x" * 41)

    def test_import_idempotent_and_skips(self) -> None:
        p = self.svc.create_project("Imp")
        folder = Path(self._tmp.name) / "photos"
        folder.mkdir()
        _write_jpg(folder / "a.jpg")
        _write_png(folder / "b.png")
        (folder / "c.cr2").write_bytes(b"raw")  # unsupported RAW
        (folder / "._a.jpg").write_bytes(b"meta")  # macOS artifact
        (folder / "note.txt").write_bytes(b"text")

        first = self.svc.import_folder(p["id"], folder)
        self.assertEqual(first["registered"], 2)
        self.assertEqual(first["skipped_raw_formats"], 1)

        second = self.svc.import_folder(p["id"], folder)
        self.assertEqual(second["registered"], 0)  # idempotent

        self.assertEqual(self.svc.get_member_count(p["id"]), 2)

    def test_rating_and_workspace_state(self) -> None:
        p = self.svc.create_project("Rate")
        folder = Path(self._tmp.name) / "r"
        folder.mkdir()
        _write_jpg(folder / "a.jpg")
        self.svc.import_folder(p["id"], folder)
        member = self.svc.list_members(p["id"])["members"][0]

        self.assertTrue(self.svc.update_rating(member["id"], 4, "red"))
        got = self.svc.get_member(member["id"])
        self.assertEqual(got["star_rating"], 4)
        self.assertEqual(got["color_label"], "red")

        self.svc.save_workspace_state(p["id"], last_member_id=member["id"])
        ws = self.svc.get_workspace_state(p["id"])
        self.assertEqual(ws["last_member_id"], member["id"])

    def test_filtering(self) -> None:
        p = self.svc.create_project("Filter")
        folder = Path(self._tmp.name) / "f"
        folder.mkdir()
        _write_jpg(folder / "one.jpg")
        _write_jpg(folder / "two.jpg")
        self.svc.import_folder(p["id"], folder)
        members = self.svc.list_members(p["id"])["members"]
        self.svc.update_rating(members[0]["id"], 3, "none")

        rated = self.svc.list_members(p["id"], rated_filter="rated")
        self.assertEqual(rated["filtered"], 1)
        star = self.svc.list_members(p["id"], star_mode="at_least", star_value=3)
        self.assertEqual(star["filtered"], 1)

    def test_remove_member_keeps_source(self) -> None:
        p = self.svc.create_project("Rm")
        folder = Path(self._tmp.name) / "rm"
        folder.mkdir()
        _write_jpg(folder / "a.jpg")
        self.svc.import_folder(p["id"], folder)
        member = self.svc.list_members(p["id"])["members"][0]
        src = member["normalized_path"]

        self.assertEqual(self.svc.remove_members([member["id"]]), 1)
        self.assertTrue(os.path.isfile(src))  # source untouched

    def test_thumbnail_and_preview_jpg(self) -> None:
        p = self.svc.create_project("Img")
        folder = Path(self._tmp.name) / "img"
        folder.mkdir()
        _write_jpg(folder / "big.jpg", size=(2000, 1500))
        self.svc.import_folder(p["id"], folder)
        member = self.svc.list_members(p["id"])["members"][0]

        thumb = self.svc.get_thumbnail_bytes(member["id"])
        self.assertIsNone(thumb.error)
        self.assertEqual(thumb.content_type, "image/jpeg")
        self.assertGreater(len(thumb.data), 0)

        preview = self.svc.get_preview_bytes(member["id"])
        self.assertIsNone(preview.error)
        self.assertGreater(len(preview.data), 0)

        # Cached second read returns identical bytes.
        again = self.svc.get_thumbnail_bytes(member["id"])
        self.assertEqual(again.data, thumb.data)

    def test_permanent_delete_two_phase(self) -> None:
        p = self.svc.create_project("Del")
        folder = Path(self._tmp.name) / "del"
        folder.mkdir()
        _write_jpg(folder / "a.jpg")
        self.svc.import_folder(p["id"], folder)
        member = self.svc.list_members(p["id"])["members"][0]
        src = member["normalized_path"]

        result = self.svc.permanent_delete([member["id"]], confirmed=True)
        self.assertEqual(result["deleted"], 1)
        self.assertFalse(os.path.isfile(src))  # physically removed
        self.assertEqual(self.svc.get_member_count(p["id"]), 0)

    def test_permanent_delete_missing_source(self) -> None:
        p = self.svc.create_project("Miss")
        folder = Path(self._tmp.name) / "miss"
        folder.mkdir()
        _write_jpg(folder / "a.jpg")
        self.svc.import_folder(p["id"], folder)
        member = self.svc.list_members(p["id"])["members"][0]
        os.unlink(member["normalized_path"])  # externally removed

        result = self.svc.permanent_delete([member["id"]], confirmed=True)
        self.assertEqual(result["already_missing"], 1)
        self.assertEqual(self.svc.get_member_count(p["id"]), 0)


class DerivedCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache = DerivedCache(Path(self._tmp.name))

    def test_put_get_roundtrip(self) -> None:
        self.cache.put(
            b"payload",
            normalized_path="c:/x.jpg",
            file_size=10,
            mtime_ns=123,
            kind="thumbnails",
        )
        got = self.cache.get(
            normalized_path="c:/x.jpg",
            file_size=10,
            mtime_ns=123,
            kind="thumbnails",
        )
        self.assertEqual(got, b"payload")

    def test_source_version_invalidation(self) -> None:
        self.cache.put(
            b"payload",
            normalized_path="c:/x.jpg",
            file_size=10,
            mtime_ns=123,
            kind="thumbnails",
        )
        # Changed mtime must miss.
        self.assertIsNone(
            self.cache.get(
                normalized_path="c:/x.jpg",
                file_size=10,
                mtime_ns=999,
                kind="thumbnails",
            )
        )

    def test_clear(self) -> None:
        self.cache.put(
            b"a",
            normalized_path="c:/a.jpg",
            file_size=1,
            mtime_ns=1,
            kind="thumbnails",
        )
        self.assertEqual(self.cache.clear(), 1)
        self.assertIsNone(
            self.cache.get(
                normalized_path="c:/a.jpg",
                file_size=1,
                mtime_ns=1,
                kind="thumbnails",
            )
        )


class CreativeLookTest(unittest.TestCase):
    def test_only_calibrated_as_shot_look_is_available(self) -> None:
        self.assertTrue(is_valid_look(DEFAULT_LOOK))
        self.assertEqual(VALID_LOOKS, (DEFAULT_LOOK,))
        for look in ("ST", "PT", "NT", "VV", "VV2", "FL", "IN", "SH", "BW", "SE"):
            with self.subTest(look=look):
                self.assertFalse(is_valid_look(look))

    def test_as_shot_noop(self) -> None:
        img = Image.new("RGB", (10, 10), color=(10, 20, 30))
        out = apply_creative_look(img, DEFAULT_LOOK)
        self.assertEqual(out.getpixel((0, 0)), (10, 20, 30))

    def test_uncalibrated_looks_are_rejected_without_mutating_base(self) -> None:
        base = Image.new("RGB", (12, 8), color=(80, 120, 160))
        original = base.tobytes()
        for look in ("ST", "VV", "BW"):
            with (
                self.subTest(look=look),
                self.assertRaisesRegex(ValueError, "trusted calibration"),
            ):
                apply_creative_look(base, look)
        self.assertEqual(base.tobytes(), original)


class DecodeSchedulerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sched = DecodeScheduler()
        self.addCleanup(self.sched.shutdown)

    def test_single_flight_decodes_once(self) -> None:
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        calls = {"n": 0}
        gate = threading.Event()

        def work() -> int:
            calls["n"] += 1
            time.sleep(0.05)
            return 42

        def caller() -> int:
            gate.wait()  # launch all four calls concurrently
            return self.sched.run_single_flight(("k",), ".jpg", "thumb", work)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(caller) for _ in range(4)]
            gate.set()
            results = [f.result() for f in futures]

        self.assertEqual(results, [42, 42, 42, 42])
        self.assertEqual(calls["n"], 1)  # decoded exactly once

    def test_distinct_keys_decode_separately(self) -> None:
        calls = {"n": 0}

        def work() -> int:
            calls["n"] += 1
            return calls["n"]

        a = self.sched.run_single_flight(("a",), ".jpg", "thumb", work)
        b = self.sched.run_single_flight(("b",), ".jpg", "thumb", work)
        self.assertEqual((a, b), (1, 2))

    def test_failure_releases_waiters_and_allows_retry(self) -> None:
        state = {"fail": True}

        def work() -> int:
            if state["fail"]:
                raise ValueError("boom")
            return 7

        with self.assertRaises(ValueError):
            self.sched.run_single_flight(("f",), ".jpg", "thumb", work)
        state["fail"] = False
        self.assertEqual(self.sched.run_single_flight(("f",), ".jpg", "thumb", work), 7)

    def test_generation_bump(self) -> None:
        g0 = self.sched.generation
        self.sched.bump_generation()
        self.assertEqual(self.sched.generation, g0 + 1)


class DecoderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_jpg_thumbnail_scaled(self) -> None:
        path = Path(self._tmp.name) / "big.jpg"
        _write_jpg(path, size=(4000, 3000))
        result = decode_thumbnail(str(path), ".jpg")
        self.assertIsNone(result.error)
        self.assertLessEqual(max(result.width, result.height), 384)

    def test_png_preserves_alpha_in_preview(self) -> None:
        path = Path(self._tmp.name) / "a.png"
        _write_png(path)
        result = decode_preview(str(path), ".png")
        self.assertIsNone(result.error)
        self.assertEqual(result.image.mode, "RGBA")

    def test_missing_file_error(self) -> None:
        result = decode_thumbnail(str(Path(self._tmp.name) / "nope.jpg"), ".jpg")
        self.assertIsNotNone(result.error)


if __name__ == "__main__":
    unittest.main()
