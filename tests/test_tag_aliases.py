from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from image_vector_service.config import ServiceConfig
from image_vector_service.federated_search import (
    LibraryCandidateSet,
    aggregate_federated_hits,
)
from image_vector_service.library_config import LibraryDefinition
from image_vector_service.models import (
    PreparedSearchCandidates,
    ResolvedSearchHit,
    SearchHit,
)
from image_vector_service.result_exporter import export_results
from image_vector_service.tag_aliases import (
    TagAliasConflictError,
    TagAliasDictionary,
    TagAliasFileError,
    TagAliasFormatError,
    TagAliasGroup,
    TagAliasStore,
)
from image_vector_service.tag_search import (
    TagCatalog,
    expand_fragments,
    matched_tags_for_result,
)


class TagAliasDictionaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zvec_tag_aliases_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_group_normalizes_nfkc_and_deduplicates_with_casefold(self) -> None:
        group = TagAliasGroup(
            " ＣＯＳＰＬＡＹ ",
            ("cosplay", " Cos ", "ＣＯＳ", "角色扮演", "角色扮演"),
        )

        self.assertEqual(group.canonical, "COSPLAY")
        self.assertEqual(group.aliases, ("Cos", "角色扮演"))

    def test_normalized_term_cannot_belong_to_two_groups(self) -> None:
        with self.assertRaises(TagAliasConflictError):
            TagAliasDictionary(
                [
                    TagAliasGroup("雷电将军", ("影", "雷神")),
                    TagAliasGroup("影", ("巴尔泽布",)),
                ]
            )

    def test_upsert_is_immutable_and_checks_conflicts(self) -> None:
        original = TagAliasDictionary([TagAliasGroup("雷电将军", ("影",))])
        updated = original.upsert("雷电将军", ("雷神", "影"))

        self.assertEqual(original.groups[0].aliases, ("影",))
        self.assertEqual(updated.groups[0].aliases, ("影", "雷神"))
        with self.assertRaises(TagAliasConflictError):
            updated.upsert("其他角色", ("雷神",))

    def test_round_trip_uses_stable_schema_and_atomic_replacement(self) -> None:
        path = self.root / "config" / "tag-aliases.json"
        aliases = TagAliasDictionary(
            [
                TagAliasGroup("雷电将军", ("雷神", "影")),
                TagAliasGroup("空", ("旅行者·空",)),
            ]
        )

        aliases.save(path)
        loaded = TagAliasDictionary.load(path)
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded.to_dict(), aliases.to_dict())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["groups"][0]["canonical"], "空")
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_bad_files_raise_specific_errors(self) -> None:
        invalid_json = self.root / "invalid.json"
        invalid_json.write_text("{", encoding="utf-8")
        with self.assertRaises(TagAliasFileError):
            TagAliasDictionary.load(invalid_json)

        wrong_schema = self.root / "wrong-schema.json"
        wrong_schema.write_text(
            json.dumps({"schema_version": 2, "groups": []}),
            encoding="utf-8",
        )
        with self.assertRaises(TagAliasFormatError):
            TagAliasDictionary.load(wrong_schema)

        conflict = self.root / "conflict.json"
        conflict.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "groups": [
                        {"canonical": "雷电将军", "aliases": ["影"]},
                        {"canonical": "影", "aliases": []},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        with self.assertRaises(TagAliasConflictError):
            TagAliasDictionary.load(conflict)

    def test_store_matches_backend_list_upsert_delete_contract(self) -> None:
        path = self.root / "tag-aliases.json"
        store = TagAliasStore.load(path)

        self.assertEqual(store.list_groups(), {"groups": [], "count": 0})
        saved = store.upsert("雷电将军", ["影", "雷神"])

        self.assertEqual(
            saved.to_dict(), {"canonical": "雷电将军", "aliases": ["影", "雷神"]}
        )
        self.assertEqual(
            store.list_groups(),
            {
                "groups": [{"canonical": "雷电将军", "aliases": ["影", "雷神"]}],
                "count": 1,
            },
        )
        self.assertEqual(TagAliasStore.load(path).list_groups(), store.list_groups())
        self.assertFalse(store.delete("影"))
        self.assertTrue(store.delete("雷电将军"))
        self.assertEqual(store.list_groups(), {"groups": [], "count": 0})


class AliasAwareTagSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.aliases = TagAliasDictionary([TagAliasGroup("雷电将军", ("雷神", "影"))])

    def test_canonical_and_every_alias_expand_to_the_same_catalog_tags(self) -> None:
        catalog = TagCatalog(
            ["雷电将军", "雷神", "影", "原神"],
            aliases=self.aliases,
        )

        plans = [catalog.resolve(query) for query in ("雷电将军", "雷神", "影")]

        self.assertTrue(
            all(
                plan.expansions[0].matches == ("影", "雷电将军", "雷神")
                for plan in plans
            )
        )
        self.assertTrue(
            all(
                plan.expansions[0].expanded_terms == ("雷电将军", "影", "雷神")
                for plan in plans
            )
        )

    def test_partial_search_still_works_without_forcing_alias_expansion(self) -> None:
        plan = expand_fragments(
            ["雷电将军", "雷神", "影", "原神"],
            "雷",
            aliases=self.aliases,
        )

        self.assertEqual(plan.expansions[0].matches, ("雷电将军", "雷神"))
        self.assertEqual(plan.expansions[0].expanded_terms, ("雷",))

    def test_matched_tags_for_result_is_exact_stable_and_alias_aware(self) -> None:
        plan = expand_fragments(
            ["雷电将军", "雷神", "影", "原神"],
            "雷电将军",
            aliases=self.aliases,
            mode="any",
        )

        matched = matched_tags_for_result(
            ["其他", "雷神", "影", "雷神"],
            plan,
        )

        self.assertEqual(matched, ("影", "雷神"))
        self.assertEqual(matched_tags_for_result(["原神"], plan), ())
        self.assertEqual(
            matched_tags_for_result(["雷电将军"], plan),
            ("雷电将军",),
        )


class MatchedTagsResultPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zvec_matched_tags_"))
        self.source = self.root / "source.jpg"
        self.source.write_bytes(b"image")
        self.library = LibraryDefinition(
            "library-a",
            "图库 A",
            self.root / "images",
            self.root / "workspace",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _candidate(self) -> ResolvedSearchHit:
        return ResolvedSearchHit(
            hit=SearchHit(
                doc_id="doc-1",
                distance=0.1,
                fields={
                    "root_id": "root",
                    "relative_path": self.source.name,
                    "sha256": "a" * 64,
                    "tags": ["雷神", "原神"],
                },
                matched_tags=("雷神",),
            ),
            source_path=str(self.source),
        )

    def test_federated_decoration_and_result_export_preserve_matched_tags(self) -> None:
        hits = aggregate_federated_hits(
            [
                LibraryCandidateSet(
                    self.library,
                    PreparedSearchCandidates(
                        query_type="text",
                        hits=[self._candidate()],
                    ),
                )
            ]
        )

        self.assertEqual(hits[0].matched_tags, ("雷神",))
        report = export_results(
            ServiceConfig(
                workspace=self.root / "workspace",
                results_directory=self.root / "results",
            ),
            query_type="text",
            hits=hits,
            top_k=1,
            query={"text": "雷电将军"},
            request_ids=[],
            usage=[],
            resolve_source=lambda _hit: self.source,
        )

        self.assertEqual(report.results[0].matched_tags, ["雷神"])
        manifest = json.loads(
            (Path(report.output_dir) / "results.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["results"][0]["matched_tags"], ["雷神"])


if __name__ == "__main__":
    unittest.main()
