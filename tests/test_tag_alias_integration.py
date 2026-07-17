from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from image_vector_service.annotation_service import (
    _proposal_matches_pending_filters,
)
from image_vector_service.backend_server import (
    _CAPABILITIES,
    BackendRequestError,
    _normalize_job,
)
from image_vector_service.config import ServiceConfig
from image_vector_service.service import ImageVectorService
from image_vector_service.tag_aliases import (
    TagAliasConflictError,
    TagAliasDictionary,
    TagAliasFileError,
    TagAliasFormatError,
    TagAliasGroup,
)


class _FakeState:
    def __init__(self, tags: tuple[str, ...]) -> None:
        self.tags = tags

    def list_effective_tags(self) -> list[str]:
        return list(self.tags)


class _RecordingRepository:
    def __init__(self) -> None:
        self.catalog_updates: list[tuple[tuple[str, ...], TagAliasDictionary]] = []

    def set_tag_catalog(
        self,
        tags: list[str],
        aliases: TagAliasDictionary | None = None,
    ) -> None:
        self.catalog_updates.append((tuple(tags), aliases or TagAliasDictionary()))

    @property
    def latest_aliases(self) -> TagAliasDictionary:
        if not self.catalog_updates:
            raise AssertionError("The service did not configure the tag catalog.")
        return self.catalog_updates[-1][1]


def _lightweight_service(
    *,
    workspace: Path,
    results_directory: Path,
    tags: tuple[str, ...] = ("雷电将军", "雷神", "影", "原神"),
) -> tuple[ImageVectorService, _RecordingRepository]:
    """Create a service facade without opening a real Zvec Collection."""

    repository = _RecordingRepository()
    service: Any = ImageVectorService.__new__(ImageVectorService)
    service.config = ServiceConfig(
        workspace=workspace,
        results_directory=results_directory,
    )
    service.state = _FakeState(tags)
    service.repository = repository
    service._refresh_tag_catalog()
    return cast(ImageVectorService, service), repository


class TagAliasBackendContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zvec_alias_backend_contract_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def normalize(
        self, command: str, params: dict[str, object] | None = None
    ) -> tuple[str, dict[str, Any]]:
        return _normalize_job(
            {"command": command, "params": params or {}},
            self.root,
        )

    def assert_invalid(
        self,
        command: str,
        params: dict[str, object],
        *,
        code: str = "invalid_params",
        message: str | None = None,
    ) -> None:
        with self.assertRaises(BackendRequestError) as caught:
            self.normalize(command, params)
        self.assertEqual(caught.exception.code, code)
        if message is not None:
            self.assertEqual(str(caught.exception), message)

    def test_alias_commands_are_advertised_and_normalize_to_stable_shapes(self) -> None:
        for command in (
            "tag_alias_list",
            "tag_alias_upsert",
            "tag_alias_delete",
        ):
            with self.subTest(command=command):
                self.assertIs(_CAPABILITIES.get(command), True)

        self.assertEqual(
            self.normalize("tag_alias_list"),
            ("tag_alias_list", {"library_id": None}),
        )
        self.assertEqual(
            self.normalize(
                "tag_alias_upsert",
                {
                    "library_id": "library-a",
                    "canonical_name": "  雷电将军  ",
                    "aliases": ["雷神", "影"],
                },
            ),
            (
                "tag_alias_upsert",
                {
                    "library_id": "library-a",
                    "canonical_name": "雷电将军",
                    "aliases": ["雷神", "影"],
                },
            ),
        )
        # ``canonical`` remains a supported compatibility spelling for older clients.
        self.assertEqual(
            self.normalize("tag_alias_delete", {"canonical": " 雷电将军 "}),
            (
                "tag_alias_delete",
                {"library_id": None, "canonical_name": "雷电将军"},
            ),
        )

    def test_alias_command_normalization_rejects_bad_or_ambiguous_inputs(self) -> None:
        self.assert_invalid(
            "tag_alias_upsert",
            {"canonical_name": ""},
            message="canonical_name must be a non-empty string.",
        )
        self.assert_invalid(
            "tag_alias_upsert",
            {"canonical_name": "雷电将军", "aliases": "影"},
            message="aliases must be an array of strings.",
        )
        self.assert_invalid(
            "tag_alias_upsert",
            {
                "canonical_name": "雷电将军",
                "canonical": "影",
                "aliases": [],
            },
            message="canonical_name and canonical cannot be supplied together.",
        )
        self.assert_invalid(
            "tag_alias_delete",
            {"canonical_name": "雷电将军", "aliases": []},
            message="tag_alias_delete does not accept aliases.",
        )
        self.assert_invalid(
            "tag_alias_list",
            {"unexpected": True},
            code="unknown_params",
        )
        self.assert_invalid(
            "tag_alias_upsert",
            {"canonical_name": "雷电将军", "aliases": ["影"] * 101},
            message="aliases can contain at most 100 items.",
        )


class SharedTagAliasServiceIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zvec_shared_alias_service_"))
        self.results = self.root / "shared-results"
        self.service_a, self.repository_a = _lightweight_service(
            workspace=self.root / "collection-a",
            results_directory=self.results,
        )
        self.service_b, self.repository_b = _lightweight_service(
            workspace=self.root / "collection-b",
            results_directory=self.results,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def manifest(self) -> Path:
        return self.results / "tag-aliases.json"

    def test_crud_is_shared_and_persists_the_public_service_contract(self) -> None:
        self.assertEqual(self.service_a.list_tag_aliases(), {"aliases": [], "count": 0})

        created = self.service_a.upsert_tag_alias(
            "  雷电将军  ",
            [" 雷神 ", "影", "影"],
        )
        self.assertEqual(
            created,
            {
                "updated": True,
                "deleted": False,
                "entry": {
                    "canonical_name": "雷电将军",
                    "aliases": ["影", "雷神"],
                },
            },
        )
        self.assertEqual(
            self.service_b.list_tag_aliases(),
            {
                "aliases": [
                    {
                        "canonical_name": "雷电将军",
                        "aliases": ["影", "雷神"],
                    }
                ],
                "count": 1,
            },
        )
        self.assertEqual(
            json.loads(self.manifest.read_text(encoding="utf-8")),
            {
                "schema_version": 1,
                "groups": [{"canonical": "雷电将军", "aliases": ["影", "雷神"]}],
            },
        )

        self.assertEqual(
            self.service_b.delete_tag_alias("  雷电将军 "),
            {"updated": False, "deleted": True, "entry": None},
        )
        self.assertEqual(
            self.service_a.list_tag_aliases(),
            {"aliases": [], "count": 0},
        )
        self.assertEqual(
            self.service_a.delete_tag_alias("雷电将军"),
            {"updated": False, "deleted": False, "entry": None},
        )

    def test_each_filtered_search_reloads_aliases_for_other_collections(self) -> None:
        initial_updates = len(self.repository_b.catalog_updates)
        self.service_a.upsert_tag_alias("雷电将军", ["影"])

        normalized = self.service_b._validate_search_tags(["影"], "any")

        self.assertEqual(normalized, ("影",))
        self.assertEqual(len(self.repository_b.catalog_updates), initial_updates + 1)
        self.assertEqual(
            self.repository_b.latest_aliases.canonical_for("影"), "雷电将军"
        )

        # Replacing the group in Collection A becomes visible to Collection B on
        # its next filtered search, without restarting either service instance.
        self.service_a.upsert_tag_alias("雷电将军", ["雷神"])
        self.service_b._validate_search_tags(["雷神"], "all")

        self.assertEqual(
            self.repository_b.latest_aliases.canonical_for("雷神"),
            "雷电将军",
        )
        self.assertIsNone(self.repository_b.latest_aliases.canonical_for("影"))
        self.assertEqual(
            self.repository_b.catalog_updates[-1][0],
            ("雷电将军", "雷神", "影", "原神"),
        )

    def test_conflicts_and_corrupt_shared_files_fail_without_overwriting_data(
        self,
    ) -> None:
        self.service_a.upsert_tag_alias("雷电将军", ["影"])
        before = self.manifest.read_bytes()

        with self.assertRaises(TagAliasConflictError):
            self.service_b.upsert_tag_alias("其他角色", ["影"])
        self.assertEqual(self.manifest.read_bytes(), before)

        with self.assertRaises(TagAliasFormatError):
            self.service_a.upsert_tag_alias("角色\x00名", [])
        self.assertEqual(self.manifest.read_bytes(), before)

        self.manifest.write_text("{", encoding="utf-8")
        with self.assertRaises(TagAliasFileError):
            self.service_a.list_tag_aliases()
        with self.assertRaises(TagAliasFileError):
            self.service_b._validate_search_tags(["影"], "any")

    def test_review_character_and_work_filters_accept_exact_aliases(self) -> None:
        aliases = TagAliasDictionary(
            [
                TagAliasGroup("雷电将军", ("雷神", "影")),
                TagAliasGroup("原神", ("Genshin",)),
            ]
        )
        proposal = {
            "doc_id": "image-1",
            "entities": {
                "character": [{"name": "雷电将军"}],
                "work": [{"name": "Genshin"}],
            },
            "fields": {},
            "identity_tags": ["雷电将军", "Genshin"],
            "low_risk_tags": [],
            "review_reasons": [],
        }
        filters = {
            "latest_index_only": False,
            "character": "影",
            "work": "原神",
            "action": "",
            "expression": "",
            "review_state": "identity",
        }

        self.assertTrue(
            _proposal_matches_pending_filters(
                proposal,
                filters,
                latest_ids=None,
                aliases=aliases,
            )
        )
        self.assertFalse(
            _proposal_matches_pending_filters(
                proposal,
                {**filters, "character": "刻晴"},
                latest_ids=None,
                aliases=aliases,
            )
        )


if __name__ == "__main__":
    unittest.main()
