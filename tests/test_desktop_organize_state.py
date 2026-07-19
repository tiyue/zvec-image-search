from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from zvec_desktop.organize_state import (
    OrganizeDataError,
    OrganizeFilters,
    OrganizeReviewSession,
    OrganizeValidationError,
    parse_alias_catalog,
    parse_alias_mutation_result,
    parse_batch_review_result,
    parse_pending_page,
    parse_review_result,
    parse_undo_result,
)


def proposal_payload(
    proposal_id: str = "proposal-001",
    *,
    source_path: str = "",
) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "doc_id": proposal_id,
        "relative_path": f"原神/刻晴/{proposal_id}.jpg",
        "source_path": source_path,
        "description": "角色站立并微笑",
        "status": "pending_review",
        "manual_tags": ["写真"],
        "folder_tags": ["原神-刻晴"],
        "existing_tags": ["写真", "原神-刻晴"],
        "proposed_tags": ["站姿", "刻晴", "原神", "写真"],
        # Deliberately includes an identity to prove the adapter removes it.
        "low_risk_tags": ["站姿", "刻晴"],
        "identity_tags": ["刻晴", "原神"],
        "tag_details": [
            {
                "tag": "写真",
                "source": "manual",
                "risk": "existing",
                "already_present": True,
            },
            {
                "tag": "原神-刻晴",
                "source": "folder",
                "source_label": "文件夹",
                "risk": "existing",
                "already_present": True,
            },
            {
                "tag": "站姿",
                "source": "model_field",
                "sources": ["model_field"],
                "risk": "low",
                "field": "pose",
                "confidence": 0.93,
            },
            {
                "tag": "刻晴",
                "source": "model_entity",
                "risk": "identity",
                "identity": True,
                "entity_type": "character",
                "requires_individual_confirmation": True,
                "confidence": 0.91,
            },
            {
                "tag": "原神",
                "source": "model_entity",
                "risk": "identity",
                "identity": True,
                "entity_type": "work",
                "requires_individual_confirmation": True,
                "confidence": 0.88,
            },
        ],
        "fields": {
            "action": {
                "values": ["action_standing"],
                "labels": ["站立"],
                "confidence": 0.92,
            },
            "expression": {
                "values": ["expression_smile"],
                "labels": ["微笑"],
                "confidence": 0.87,
            },
        },
        "entities": {
            "real_person": [],
            "cosplayer": [],
            "character": [
                {
                    "name": "刻晴",
                    "state": "suggested",
                    "evidence": ["folder_name", "visual"],
                    "confidence": 0.91,
                }
            ],
            "work": {
                "name": "原神",
                "state": "suggested",
                "evidence": ["folder_name"],
                "confidence": 0.88,
            },
        },
        "review_required": True,
        "review_reasons": ["entity:character:suggested"],
    }


def page_payload(
    proposals: list[dict[str, Any]],
    *,
    pending_count: int | None = None,
    offset: int = 0,
    limit: int = 100,
    has_more: bool | None = None,
    undo_available: bool = False,
) -> dict[str, Any]:
    count = len(proposals) if pending_count is None else pending_count
    more = offset + len(proposals) < count if has_more is None else has_more
    return {
        "pending_count": count,
        "total_count": count,
        "offset": offset,
        "limit": limit,
        "proposals": proposals,
        "has_more": more,
        "undo_available": undo_available,
        "filters": {
            "latest_index_only": False,
            "character": "",
            "work": "",
            "action": "",
            "expression": "",
            "review_state": "all",
        },
    }


class OrganizePayloadAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "刻晴.jpg"
        self.image.write_bytes(b"thumbnail-source")

    def test_pending_proposal_exposes_image_sources_and_tag_difference(self) -> None:
        page = parse_pending_page(
            page_payload([proposal_payload(source_path=str(self.image))])
        )
        item = page.proposals[0]

        self.assertEqual(item.proposal_id, "proposal-001")
        self.assertEqual(item.source_path, self.image)
        self.assertTrue(item.thumbnail.available)
        self.assertEqual(item.thumbnail.status, "ready")
        self.assertEqual(item.existing_tags, ("写真", "原神-刻晴"))
        self.assertEqual(
            item.difference.suggested_additions,
            ("站姿", "刻晴", "原神"),
        )
        self.assertEqual(item.difference.low_risk_additions, ("站姿",))
        self.assertEqual(item.difference.identity_additions, ("刻晴", "原神"))
        self.assertEqual(item.difference.already_present_suggestions, ("写真",))

        by_tag = {detail.tag: detail for detail in item.tag_details}
        self.assertEqual(by_tag["写真"].effective_source_label, "人工标签")
        self.assertEqual(by_tag["原神-刻晴"].effective_source_label, "文件夹")
        self.assertEqual(by_tag["站姿"].effective_source_label, "模型字段")
        self.assertEqual(by_tag["刻晴"].effective_source_label, "模型身份")

    def test_identity_is_never_batch_safe_even_if_backend_marks_it_low_risk(
        self,
    ) -> None:
        item = parse_pending_page(page_payload([proposal_payload()])).proposals[0]

        self.assertEqual(item.low_risk_tags, ("站姿", "刻晴"))
        self.assertEqual(item.batch_safe_tags, ("站姿",))
        self.assertEqual(item.review_draft_tags, ("站姿",))
        self.assertTrue(item.can_batch_accept)
        self.assertEqual(
            [(choice.tag, choice.entity_type) for choice in item.identity_choices],
            [("刻晴", "character"), ("原神", "work")],
        )

    def test_existing_low_risk_identity_is_not_pending_confirmation(self) -> None:
        raw = proposal_payload()
        raw["existing_tags"] = ["写真", "原神-刻晴", "刻晴"]
        raw["proposed_tags"] = ["站姿"]
        raw["identity_tags"] = []
        raw["low_risk_tags"] = ["站姿", "刻晴"]
        raw["tag_details"] = [
            *(
                detail
                for detail in raw["tag_details"]
                if detail["tag"] not in {"刻晴", "原神"}
            ),
            {
                "tag": "刻晴",
                "source": "accepted_auto",
                "risk": "low",
                "identity": True,
                "entity_type": "character",
                "already_present": True,
                "requires_individual_confirmation": False,
                "confidence": 0.94,
            },
        ]
        raw["entities"]["character"][0]["state"] = "confirmed"
        raw["entities"]["work"] = []

        item = parse_pending_page(page_payload([raw])).proposals[0]

        self.assertEqual(item.identity_tags, ())
        self.assertNotIn("identity", item.review_buckets)
        self.assertEqual(item.identity_choices, ())
        self.assertEqual(item.batch_safe_tags, ("站姿",))

    def test_entity_names_are_a_second_identity_safety_boundary(self) -> None:
        raw = proposal_payload()
        raw["identity_tags"] = []
        raw["tag_details"] = [
            detail
            for detail in raw["tag_details"]
            if detail["tag"] not in {"刻晴", "原神"}
        ]
        raw["low_risk_tags"] = ["站姿", "刻晴", "原神"]

        item = parse_pending_page(page_payload([raw])).proposals[0]

        self.assertEqual(item.identity_tags, ("刻晴", "原神"))
        self.assertEqual(item.batch_safe_tags, ("站姿",))

    def test_missing_source_file_keeps_a_non_crashing_thumbnail_placeholder(
        self,
    ) -> None:
        missing = self.root / "missing.jpg"
        item = parse_pending_page(
            page_payload([proposal_payload(source_path=str(missing))])
        ).proposals[0]

        self.assertFalse(item.thumbnail.available)
        self.assertEqual(item.thumbnail.status, "missing_file")
        self.assertEqual(item.source_path, missing)

    def test_filters_cover_latest_character_work_action_expression_and_state(
        self,
    ) -> None:
        filters = OrganizeFilters(
            latest_index_only=True,
            character=" 刻 ",
            work=" 神 ",
            action=" 站 ",
            expression=" 笑 ",
            review_state="identity",
        )
        item = parse_pending_page(page_payload([proposal_payload()])).proposals[0]
        backend = filters.to_backend_filters().to_params()

        self.assertEqual(
            backend,
            {
                "latest_index_only": True,
                "character": "刻",
                "work": "神",
                "action": "站",
                "expression": "笑",
                "review_state": "identity",
            },
        )
        self.assertTrue(filters.matches_loaded(item))
        self.assertFalse(OrganizeFilters(character="甘雨").matches_loaded(item))
        self.assertFalse(OrganizeFilters(review_state="low_risk").matches_loaded(item))

    def test_pagination_tracks_large_pending_library_in_one_hundred_item_pages(
        self,
    ) -> None:
        page = parse_pending_page(
            page_payload(
                [proposal_payload("proposal-101")],
                pending_count=2074,
                offset=100,
                limit=100,
                has_more=True,
                undo_available=True,
            )
        )
        pagination = page.pagination

        self.assertEqual(pagination.current_page, 2)
        self.assertEqual(pagination.page_count, 21)
        self.assertEqual(pagination.first_item_number, 101)
        self.assertEqual(pagination.last_item_number, 101)
        self.assertEqual(pagination.previous_offset, 0)
        self.assertEqual(pagination.next_offset, 200)
        self.assertEqual(pagination.last_valid_offset(), 2000)
        self.assertTrue(page.undo_available)

    def test_corrupt_proposal_is_isolated_with_a_structured_issue(self) -> None:
        damaged = proposal_payload("damaged")
        damaged["low_risk_tags"] = "站姿"
        payload = page_payload([proposal_payload("valid"), damaged])

        page = parse_pending_page(payload)

        self.assertEqual([item.proposal_id for item in page.proposals], ["valid"])
        self.assertEqual(page.skipped_count, 1)
        issue = page.issues[0]
        self.assertEqual(issue.code, "invalid_organize_payload")
        self.assertEqual(issue.path, "proposals[1].low_risk_tags")
        self.assertEqual(issue.to_dict()["path"], issue.path)

        with self.assertRaises(OrganizeDataError) as strict_error:
            parse_pending_page(payload, strict=True)
        self.assertEqual(
            strict_error.exception.path,
            "proposals[1].low_risk_tags",
        )

    def test_corrupt_pagination_fails_structurally_instead_of_guessing(self) -> None:
        payload = page_payload([proposal_payload()], pending_count=1)
        payload["has_more"] = True

        with self.assertRaises(OrganizeDataError) as raised:
            parse_pending_page(payload)

        self.assertEqual(raised.exception.path, "has_more")
        self.assertEqual(raised.exception.to_dict()["code"], "invalid_organize_payload")


class OrganizeReviewSessionTest(unittest.TestCase):
    def test_unchecked_identity_cannot_leak_through_ordinary_edited_tags(self) -> None:
        page = parse_pending_page(page_payload([proposal_payload()]))
        session = OrganizeReviewSession(page)
        session.set_edited_tags("proposal-001", ("站姿", "刻晴", "原神"))
        session.confirm_identity("proposal-001", "刻晴")

        request = session.build_review_request("library-a")
        params = request.to_params()
        decision = params["decisions"][0]

        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["accepted_tags"], ["站姿", "刻晴"])
        self.assertEqual(decision["confirmed_identity_tags"], ["刻晴"])
        self.assertNotIn("原神", decision["accepted_tags"])

        identity_request = session.build_identity_confirmation_request(
            "library-a", "proposal-001"
        ).to_params()
        identity_decision = identity_request["decisions"][0]
        self.assertEqual(identity_decision["confirmed_identity_tags"], ["刻晴"])

    def test_unknown_or_unchecked_identity_is_rejected_locally(self) -> None:
        session = OrganizeReviewSession(
            parse_pending_page(page_payload([proposal_payload()]))
        )
        with self.assertRaises(OrganizeValidationError) as unknown:
            session.confirm_identity("proposal-001", "甘雨")
        self.assertEqual(unknown.exception.code, "unknown_identity_tag")

        with self.assertRaises(OrganizeValidationError) as missing:
            session.build_identity_confirmation_request("library-a", "proposal-001")
        self.assertEqual(missing.exception.code, "identity_confirmation_required")

    def test_one_full_page_can_prepare_one_hundred_safe_batch_items(self) -> None:
        proposals: list[dict[str, Any]] = []
        for index in range(100):
            raw = proposal_payload(f"proposal-{index:03}")
            proposals.append(raw)
        page = parse_pending_page(page_payload(proposals, limit=100))
        session = OrganizeReviewSession(page)

        selected = session.select_all_low_risk()
        request = session.build_low_risk_batch_request("library-a")
        params = request.to_params()
        progress = session.progress

        self.assertEqual(selected, 100)
        self.assertEqual(len(params["proposal_ids"]), 100)
        self.assertEqual(len(params["accepted_tags_by_proposal"]), 100)
        self.assertTrue(params["exclude_identity_tags"])
        self.assertTrue(
            all(
                tags == ["站姿"]
                for tags in params["accepted_tags_by_proposal"].values()
            )
        )
        self.assertEqual(progress.reviewed_items, 100)
        self.assertEqual(progress.batch_selected_items, 100)
        self.assertEqual(progress.target_fraction, 1.0)
        self.assertTrue(progress.target_reached)

    def test_failed_item_uses_manual_tags_and_cannot_enter_batch(self) -> None:
        raw = proposal_payload("failed")
        raw.update(
            {
                "status": "failed",
                "manual_tags": [],
                "existing_tags": [],
                "proposed_tags": [],
                "low_risk_tags": [],
                "identity_tags": [],
                "tag_details": [],
                "entities": {},
                "error": "model request failed",
                "failure_category": "retryable",
            }
        )
        session = OrganizeReviewSession(parse_pending_page(page_payload([raw])))

        with self.assertRaises(OrganizeValidationError):
            session.select_low_risk("failed")
        session.set_decision("failed", "manual")
        session.set_edited_tags("failed", ("待补标", "人物"))
        params = session.build_review_request("library-a").to_params()
        decision = params["decisions"][0]
        self.assertEqual(decision["decision"], "manual")
        self.assertEqual(decision["accepted_tags"], ["待补标", "人物"])

    def test_batch_and_undo_results_update_session_undo_state(self) -> None:
        session = OrganizeReviewSession(
            parse_pending_page(page_payload([proposal_payload()]))
        )
        batch = session.record_batch_result(
            {
                "batch_id": "batch-001",
                "accepted": 1,
                "updated": 1,
                "identity_excluded": 2,
                "identity_excluded_count": 1,
                "identity_exclusions": [
                    {
                        "proposal_id": "proposal-001",
                        "tags": ["刻晴", "原神"],
                    }
                ],
                "undo_available": True,
            }
        )

        self.assertEqual(batch.identity_excluded_count, 2)
        self.assertTrue(session.undo_available)
        undone = session.record_undo_result(
            {
                "undone": True,
                "updated": 1,
                "batch_id": "batch-001",
                "undo_available": False,
            }
        )
        self.assertTrue(undone.undone)
        self.assertFalse(session.undo_available)

    def test_discard_restores_clean_page_state(self) -> None:
        session = OrganizeReviewSession(
            parse_pending_page(page_payload([proposal_payload()]))
        )
        session.set_decision("proposal-001", "reject")
        self.assertTrue(session.has_unsaved_changes)

        session.discard_changes()

        self.assertFalse(session.has_unsaved_changes)
        self.assertEqual(session.state_for("proposal-001").decision, "pending")


class AliasCatalogAdapterTest(unittest.TestCase):
    def test_real_backend_alias_shape_is_sorted_displayed_and_resolved(self) -> None:
        catalog = parse_alias_catalog(
            {
                "aliases": [
                    {"canonical_name": "雷电将军", "aliases": ["雷神", "影"]},
                    {"canonical_name": "刻晴", "aliases": ["阿晴"]},
                ],
                "count": 2,
            }
        )

        self.assertEqual(catalog.count, 2)
        self.assertEqual(catalog.canonical_for("雷神"), "雷电将军")
        self.assertEqual(catalog.equivalent_terms("影"), ("雷电将军", "雷神", "影"))
        self.assertIn("→", catalog.entries[1].display_text)

    def test_legacy_alias_shape_and_edit_validation_are_supported(self) -> None:
        catalog = parse_alias_catalog(
            {"items": [{"canonical": "雷电将军", "aliases": ["雷神", "影"]}]}
        )
        replacement = catalog.validate_upsert("雷电将军", ("雷神", "巴尔泽布"))

        self.assertEqual(replacement.aliases, ("雷神", "巴尔泽布"))
        with self.assertRaises(OrganizeValidationError) as empty:
            catalog.validate_upsert("雷电将军", ("雷电将军",))
        self.assertEqual(empty.exception.code, "alias_required")

    def test_alias_conflicts_and_count_corruption_are_structured(self) -> None:
        with self.assertRaises(OrganizeDataError) as conflict:
            parse_alias_catalog(
                {
                    "groups": [
                        {"canonical": "雷电将军", "aliases": ["影"]},
                        {"canonical": "影", "aliases": ["巴尔泽布"]},
                    ]
                }
            )
        self.assertEqual(conflict.exception.code, "alias_conflict")

        with self.assertRaises(OrganizeDataError) as count:
            parse_alias_catalog(
                {
                    "aliases": [{"canonical_name": "雷电将军", "aliases": ["影"]}],
                    "count": 2,
                }
            )
        self.assertEqual(count.exception.path, "alias_result.count")

    def test_alias_catalog_can_drive_fuzzy_identity_filters(self) -> None:
        catalog = parse_alias_catalog(
            {
                "aliases": [
                    {"canonical_name": "刻晴", "aliases": ["阿晴"]},
                    {"canonical_name": "原神", "aliases": ["Genshin"]},
                ]
            }
        )
        item = parse_pending_page(page_payload([proposal_payload()])).proposals[0]

        self.assertTrue(
            OrganizeFilters(character="阿晴", work="Genshin").matches_loaded(
                item, catalog
            )
        )


class OperationResultValidationTest(unittest.TestCase):
    def test_standalone_result_parsers_reject_damaged_fields(self) -> None:
        batch = parse_batch_review_result(
            {
                "accepted": 2,
                "updated": 2,
                "undo_available": True,
            }
        )
        self.assertEqual(batch.accepted, 2)

        undo = parse_undo_result(
            {
                "undone": False,
                "updated": 0,
                "batch_id": "",
                "undo_available": False,
            }
        )
        self.assertFalse(undo.undone)

        with self.assertRaises(OrganizeDataError) as raised:
            parse_batch_review_result(
                {
                    "accepted": True,
                    "updated": 1,
                    "undo_available": True,
                }
            )
        self.assertEqual(raised.exception.path, "batch_result.accepted")

    def test_review_and_alias_mutation_results_match_backend_shapes(self) -> None:
        review = parse_review_result(
            {
                "accepted": 2,
                "rejected": 1,
                "updated": 3,
                "failed": 1,
                "failures": [{"doc_id": "proposal-004", "error": "source changed"}],
                "pending_count": 27,
            }
        )
        self.assertEqual(review.updated, 3)
        self.assertEqual(review.failures[0].proposal_id, "proposal-004")
        self.assertFalse(review.undo_available)

        upserted = parse_alias_mutation_result(
            {
                "updated": True,
                "deleted": False,
                "entry": {
                    "canonical_name": "雷电将军",
                    "aliases": ["雷神", "影"],
                },
            }
        )
        self.assertTrue(upserted.updated)
        assert upserted.entry is not None
        self.assertEqual(upserted.entry.aliases, ("雷神", "影"))

        deleted = parse_alias_mutation_result(
            {"updated": False, "deleted": True, "entry": None}
        )
        self.assertTrue(deleted.deleted)
        self.assertIsNone(deleted.entry)


if __name__ == "__main__":
    unittest.main()
