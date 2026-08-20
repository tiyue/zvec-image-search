from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from image_vector_service.state import ENTRY_COLUMNS, IndexState
from image_vector_service.tag_rank_query import ranked_tag_candidates
from image_vector_service.tag_search import TagCatalog, tag_match_confidence


def _entry(doc_id: str, relative_path: str, tags: list[str]) -> dict[str, object]:
    return {
        "doc_id": doc_id,
        "root_id": "root-a",
        "relative_path": relative_path,
        "parent_directory": str(Path(relative_path).parent).replace("\\", "/"),
        "file_name": Path(relative_path).name,
        "extension": "jpg",
        "mime_type": "image/jpeg",
        "sha256": f"{int(doc_id.split('-')[-1]) + 1:064x}",
        "size_bytes": 100,
        "mtime_ns": 1,
        "width": 32,
        "height": 32,
        "tags": tags,
        "folder_tags": [],
        "accepted_auto_tags": [],
        "inherited_tags": [],
    }


class RankedTagQueryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = IndexState(Path(self.temporary.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_sql_top_n_matches_python_scoring_and_reports_total(self) -> None:
        entries = [
            _entry("doc-0", "exact.jpg", ["原"]),
            _entry("doc-1", "prefix.jpg", ["原神"]),
            _entry("doc-2", "contains.jpg", ["超原神"]),
            _entry("doc-3", "other.jpg", ["崩坏"]),
        ]
        self.state.set_many(entries)
        catalog = TagCatalog([tag for entry in entries for tag in entry["tags"]])
        query = catalog.resolve("原")
        no_filter = catalog.resolve(None)

        page = ranked_tag_candidates(
            self.state.connection,
            query,
            no_filter,
            limit=2,
        )

        self.assertEqual(page.total, 3)
        self.assertEqual([item.doc_id for item in page.items], ["doc-0", "doc-1"])
        by_id = {str(entry["doc_id"]): entry for entry in entries}
        for item in page.items:
            self.assertAlmostEqual(
                item.confidence,
                tag_match_confidence(query, by_id[item.doc_id]["tags"]),
            )

    def test_filter_all_and_any_semantics_stay_inside_sql(self) -> None:
        entries = [
            _entry("doc-0", "both.jpg", ["原神", "角色", "作品"]),
            _entry("doc-1", "character.jpg", ["原神", "角色"]),
            _entry("doc-2", "work.jpg", ["原神", "作品"]),
        ]
        self.state.set_many(entries)
        catalog = TagCatalog(["原神", "角色", "作品"])
        query = catalog.resolve("原")

        all_page = ranked_tag_candidates(
            self.state.connection,
            query,
            catalog.resolve(["角色", "作品"], mode="all"),
            limit=10,
        )
        any_page = ranked_tag_candidates(
            self.state.connection,
            query,
            catalog.resolve(["角色", "作品"], mode="any"),
            limit=10,
        )

        self.assertEqual([item.doc_id for item in all_page.items], ["doc-0"])
        self.assertEqual(any_page.total, 3)

    def test_json_terms_avoid_sqlite_parameter_limit(self) -> None:
        tags = [f"原神角色-{index:04d}" for index in range(1_200)]
        self.state.set_many([_entry("doc-0", "wide.jpg", [tags[-1]])])
        catalog = TagCatalog(tags)
        query = catalog.resolve("原神角色", max_expansions_per_fragment=2_000)

        page = ranked_tag_candidates(
            self.state.connection,
            query,
            catalog.resolve(None),
            limit=10,
        )

        self.assertEqual(page.total, 1)
        self.assertEqual([item.doc_id for item in page.items], ["doc-0"])

    def test_legacy_tag_index_is_rebuilt_as_a_covering_index(self) -> None:
        path = self.state.path
        self.state.close()
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                """
                DROP INDEX idx_entry_tag_index_tag;
                CREATE INDEX idx_entry_tag_index_tag ON entry_tag_index(tag);
                """
            )
            connection.commit()
        finally:
            connection.close()

        self.state = IndexState(path)
        columns = tuple(
            str(row["name"])
            for row in self.state.connection.execute(
                "PRAGMA index_info('idx_entry_tag_index_tag')"
            )
        )
        self.assertEqual(columns, ("tag", "doc_id"))

    def test_large_match_set_materializes_only_requested_rows(self) -> None:
        placeholders = ", ".join("?" for _ in ENTRY_COLUMNS)
        total = 100_000

        def rows():
            for index in range(total):
                yield self.state._entry_values(
                    _entry(
                        f"doc-{index}",
                        f"bulk/{index:06d}.jpg",
                        ["原神"],
                    )
                )

        with self.state.connection:
            self.state.connection.executemany(
                f"INSERT INTO entries({', '.join(ENTRY_COLUMNS)}) "
                f"VALUES({placeholders})",
                rows(),
            )
            self.state.connection.executemany(
                "INSERT INTO entry_tag_index(doc_id, tag) VALUES(?, '原神')",
                ((f"doc-{index}",) for index in range(total)),
            )

        materialized = 0
        original_factory = self.state.connection.row_factory

        def counting_factory(
            cursor: sqlite3.Cursor,
            row: tuple[object, ...],
        ) -> dict[str, object]:
            nonlocal materialized
            materialized += 1
            return {
                description[0]: value
                for description, value in zip(cursor.description, row, strict=True)
            }

        self.state.connection.row_factory = counting_factory
        try:
            catalog = TagCatalog(["原神"])
            page = ranked_tag_candidates(
                self.state.connection,
                catalog.resolve("原"),
                catalog.resolve(None),
                limit=25,
            )
        finally:
            self.state.connection.row_factory = original_factory

        self.assertEqual(page.total, total)
        self.assertEqual(len(page.items), 25)
        self.assertEqual(materialized, 25)


if __name__ == "__main__":
    unittest.main()
