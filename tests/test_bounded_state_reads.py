from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from image_vector_service.state import IndexState

NOISE_COUNT = 100_000
TARGET_COUNT = 1_203
PAGE_SIZE = 500
SHARED_SHA = "f" * 64


def _row(
    doc_id: str,
    root_id: str,
    relative_path: str,
    sha256: str,
) -> tuple[object, ...]:
    file_name = Path(relative_path).name
    return (
        doc_id,
        root_id,
        relative_path,
        Path(relative_path).parent.as_posix(),
        file_name,
        ".jpg",
        "image/jpeg",
        sha256,
        100,
        1,
        32,
        32,
        "[]",
        "[]",
        "[]",
        "[]",
    )


class BoundedStateReadScaleTest(unittest.TestCase):
    temporary: ClassVar[tempfile.TemporaryDirectory[str]]
    state: ClassVar[IndexState]

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.state = IndexState(Path(cls.temporary.name) / "state.sqlite3")
        columns = (
            "doc_id, root_id, relative_path, parent_directory, file_name, "
            "extension, mime_type, sha256, size_bytes, mtime_ns, width, height, "
            "tags_json, folder_tags_json, accepted_auto_tags_json, "
            "inherited_tags_json"
        )

        def noise_rows() -> object:
            for index in range(NOISE_COUNT):
                yield _row(
                    f"noise-{index:06d}",
                    "root-z-noise",
                    f"noise/{index:06d}.jpg",
                    f"{index:064x}",
                )

        def target_rows() -> object:
            for index in range(TARGET_COUNT):
                sha256 = (
                    SHARED_SHA if index in {1, 2} else f"{NOISE_COUNT + index:064x}"
                )
                yield _row(
                    f"target-{index:06d}",
                    "root-a-target",
                    f"album/{index:06d}.jpg",
                    sha256,
                )

        with cls.state.connection:
            cls.state.connection.executemany(
                f"INSERT INTO entries({columns}) VALUES(?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?)",
                noise_rows(),
            )
            cls.state.connection.executemany(
                f"INSERT INTO entries({columns}) VALUES(?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?)",
                target_rows(),
            )
            cls.state.connection.executemany(
                "INSERT INTO document_annotations("
                "doc_id, source_sha256, status, description) VALUES(?, ?, ?, ?)",
                (
                    (
                        f"target-{index:06d}",
                        SHARED_SHA
                        if index in {1, 2}
                        else f"{NOISE_COUNT + index:064x}",
                        "accepted",
                        "bounded join fixture",
                    )
                    for index in range(0, TARGET_COUNT, 10)
                ),
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.state.close()
        cls.temporary.cleanup()

    def test_root_iterator_uses_keyset_pages_bounded_to_500_rows(self) -> None:
        statements: list[str] = []
        self.state.connection.set_trace_callback(statements.append)
        try:
            pages = list(
                self.state.iter_entries_for_root(
                    "root-a-target",
                    chunk_size=PAGE_SIZE,
                )
            )
        finally:
            self.state.connection.set_trace_callback(None)

        self.assertEqual([len(page) for page in pages], [500, 500, 203])
        self.assertTrue(all(len(page) <= PAGE_SIZE for page in pages))
        self.assertEqual(
            [entry["doc_id"] for page in pages for entry in page][:2],
            ["target-000000", "target-000001"],
        )
        selects = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        ]
        self.assertEqual(len(selects), len(pages) + 1)

        plan = " ".join(
            str(row["detail"])
            for row in self.state.connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM entries WHERE root_id = ? "
                "AND (relative_path, doc_id) > (?, ?) "
                "ORDER BY relative_path, doc_id LIMIT ?",
                ("root-a-target", "", "", PAGE_SIZE),
            )
        )
        self.assertIn("idx_entries_auto_tag_page", plan)

    def test_joined_iterator_reads_100k_library_without_n_plus_one(self) -> None:
        statements: list[str] = []
        total = 0
        annotated = 0
        pages = 0
        self.state.connection.set_trace_callback(statements.append)
        try:
            for page in self.state.iter_entries_with_annotations(chunk_size=PAGE_SIZE):
                pages += 1
                self.assertLessEqual(len(page), PAGE_SIZE)
                total += len(page)
                annotated += sum(annotation is not None for _entry, annotation in page)
        finally:
            self.state.connection.set_trace_callback(None)

        expected_total = NOISE_COUNT + TARGET_COUNT
        self.assertEqual(total, expected_total)
        self.assertEqual(annotated, math.ceil(TARGET_COUNT / 10))
        self.assertEqual(pages, math.ceil(expected_total / PAGE_SIZE))
        selects = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
        ]
        self.assertEqual(len(selects), pages + 1)

        plan = " ".join(
            str(row["detail"])
            for row in self.state.connection.execute(
                "EXPLAIN QUERY PLAN SELECT entries.doc_id, annotations.doc_id "
                "FROM entries LEFT JOIN document_annotations AS annotations "
                "ON annotations.doc_id = entries.doc_id "
                "WHERE (entries.root_id, entries.relative_path, entries.doc_id) "
                "> (?, ?, ?) "
                "ORDER BY entries.root_id, entries.relative_path, entries.doc_id "
                "LIMIT ?",
                ("", "", "", PAGE_SIZE),
            )
        )
        self.assertIn("idx_entries_auto_tag_page", plan)
        self.assertIn("document_annotations", plan)

    def test_bulk_sha_lookup_is_one_select_and_uses_covering_index(self) -> None:
        known_sha = f"{NOISE_COUNT + 10:064x}"
        missing_sha = "e" * 64
        statements: list[str] = []
        read_conn = self.state._read_connection()
        read_conn.set_trace_callback(statements.append)
        try:
            matches = self.state.find_doc_ids_by_sha_many(
                [SHARED_SHA, known_sha, missing_sha, SHARED_SHA]
            )
        finally:
            read_conn.set_trace_callback(None)

        self.assertEqual(
            matches,
            {
                SHARED_SHA: "target-000001",
                known_sha: "target-000010",
            },
        )
        self.assertEqual(len(statements), 1)
        plan = " ".join(
            str(row["detail"])
            for row in self.state.connection.execute(
                "EXPLAIN QUERY PLAN "
                "WITH requested(sha256) AS ("
                "SELECT DISTINCT CAST(value AS TEXT) FROM json_each(?)"
                ") SELECT requested.sha256, MIN(entries.doc_id) AS doc_id "
                "FROM requested JOIN entries "
                "ON entries.sha256 = requested.sha256 "
                "GROUP BY requested.sha256",
                (json.dumps([SHARED_SHA, known_sha]),),
            )
        )
        self.assertIn("COVERING INDEX idx_entries_sha256", plan)


class StateIndexUpgradeTest(unittest.TestCase):
    def test_legacy_hash_index_is_upgraded_to_cover_doc_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.sqlite3"
            state = IndexState(path)
            state.close()
            connection = sqlite3.connect(path)
            try:
                connection.executescript(
                    "DROP INDEX idx_entries_sha256; "
                    "CREATE INDEX idx_entries_sha256 ON entries(sha256);"
                )
                connection.commit()
            finally:
                connection.close()

            reopened = IndexState(path)
            try:
                columns = tuple(
                    str(row["name"])
                    for row in reopened.connection.execute(
                        "PRAGMA index_info('idx_entries_sha256')"
                    )
                )
                self.assertEqual(columns, ("sha256", "doc_id"))
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
