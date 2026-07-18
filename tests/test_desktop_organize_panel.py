from __future__ import annotations

import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from concurrent.futures import Future
from contextlib import suppress
from pathlib import Path
from tkinter import ttk
from typing import Any

from PIL import Image

from zvec_desktop.library_tasks import (
    AutoTagPendingRequest,
    AutoTagReviewRequest,
    AutoTagUndoRequest,
    IdentityConfirmationRequest,
    LowRiskBatchReviewRequest,
    TagAliasDeleteRequest,
    TagAliasListRequest,
    TagAliasUpsertRequest,
)
from zvec_desktop.organize_panel import OrganizePanel, parse_tag_text
from zvec_desktop.widgets import ImageTaskDispatcher


def _proposal(proposal_id: str, source_path: Path) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "relative_path": f"原神/刻晴/{proposal_id}.png",
        "source_path": str(source_path),
        "description": "角色站立并微笑",
        "status": "pending_review",
        "existing_tags": ["写真"],
        "manual_tags": ["写真"],
        "folder_tags": ["原神-刻晴"],
        "proposed_tags": ["站姿", "刻晴", "原神"],
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
                "tag": "站姿",
                "source": "model_field",
                "risk": "low",
                "field": "pose",
                "confidence": 0.94,
            },
            {
                "tag": "刻晴",
                "source": "model_entity",
                "risk": "identity",
                "identity": True,
                "entity_type": "character",
                "requires_individual_confirmation": True,
            },
            {
                "tag": "原神",
                "source": "model_entity",
                "risk": "identity",
                "identity": True,
                "entity_type": "work",
                "requires_individual_confirmation": True,
            },
        ],
        "fields": {
            "action": {
                "values": ["action_standing"],
                "labels": ["站立"],
                "confidence": 0.9,
            },
            "expression": {
                "values": ["expression_smile"],
                "labels": ["微笑"],
                "confidence": 0.88,
            },
        },
        "entities": {
            "character": [{"name": "刻晴", "state": "suggested"}],
            "work": [{"name": "原神", "state": "suggested"}],
        },
        "review_reasons": ["entity:character:suggested"],
    }


class FakeOrganizeOperations:
    def __init__(self, source_path: Path, *, page_items: int = 2) -> None:
        self.source_path = source_path
        self.page_items = page_items
        self.pending_count = page_items
        self.undo_available = True
        self.calls: list[tuple[str, object]] = []
        self.deferred_pending: Future[object] | None = None
        self.use_deferred_pending = False
        self.aliases: dict[str, Any] = {
            "aliases": [
                {"canonical_name": "雷电将军", "aliases": ["雷神", "影"]},
                {"canonical_name": "刻晴", "aliases": ["阿晴"]},
            ],
            "count": 2,
        }

    @staticmethod
    def _completed(value: object) -> Future[object]:
        future: Future[object] = Future()
        future.set_result(value)
        return future

    def _pending_payload(self, request: AutoTagPendingRequest) -> dict[str, Any]:
        start = request.offset
        available = max(0, self.pending_count - start)
        count = min(request.limit, available)
        proposals = [
            _proposal(f"proposal-{start + index:03}", self.source_path)
            for index in range(count)
        ]
        return {
            "pending_count": self.pending_count,
            "total_count": self.pending_count,
            "offset": request.offset,
            "limit": request.limit,
            "proposals": proposals,
            "has_more": request.offset + len(proposals) < self.pending_count,
            "undo_available": self.undo_available,
            "filters": request.filters.to_params(),
        }

    def load_pending(self, request: AutoTagPendingRequest) -> Future[object]:
        self.calls.append(("load_pending", request))
        if self.use_deferred_pending:
            self.deferred_pending = Future()
            return self.deferred_pending
        return self._completed(self._pending_payload(request))

    def submit_review(self, request: AutoTagReviewRequest) -> Future[object]:
        self.calls.append(("submit_review", request))
        return self._completed(
            {
                "accepted": len(request.decisions),
                "rejected": 0,
                "updated": len(request.decisions),
                "failed": 0,
                "failures": [],
                "pending_count": max(0, self.pending_count - len(request.decisions)),
            }
        )

    def submit_low_risk_batch(
        self, request: LowRiskBatchReviewRequest
    ) -> Future[object]:
        self.calls.append(("submit_low_risk_batch", request))
        return self._completed(
            {
                "batch_id": "batch-001",
                "accepted": len(request.proposal_ids),
                "updated": len(request.proposal_ids),
                "identity_excluded_count": 2 * len(request.proposal_ids),
                "identity_exclusions": [],
                "undo_available": True,
            }
        )

    def submit_identity_confirmation(
        self, request: IdentityConfirmationRequest
    ) -> Future[object]:
        self.calls.append(("submit_identity_confirmation", request))
        return self._completed(
            {
                "accepted": 1,
                "rejected": 0,
                "updated": 1,
                "failed": 0,
                "failures": [],
                "pending_count": max(0, self.pending_count - 1),
            }
        )

    def undo_low_risk_batch(self, request: AutoTagUndoRequest) -> Future[object]:
        self.calls.append(("undo_low_risk_batch", request))
        return self._completed(
            {
                "undone": True,
                "updated": 2,
                "batch_id": "batch-001",
                "undo_available": False,
            }
        )

    def list_aliases(self, request: TagAliasListRequest) -> Future[object]:
        self.calls.append(("list_aliases", request))
        return self._completed(self.aliases)

    def upsert_alias(self, request: TagAliasUpsertRequest) -> Future[object]:
        self.calls.append(("upsert_alias", request))
        self.aliases = {
            "aliases": [
                *[
                    entry
                    for entry in self.aliases["aliases"]
                    if entry["canonical_name"] != request.canonical_name
                ],
                {
                    "canonical_name": request.canonical_name,
                    "aliases": list(request.aliases),
                },
            ]
        }
        self.aliases["count"] = len(self.aliases["aliases"])
        return self._completed(
            {
                "updated": True,
                "deleted": False,
                "entry": {
                    "canonical_name": request.canonical_name,
                    "aliases": list(request.aliases),
                },
            }
        )

    def delete_alias(self, request: TagAliasDeleteRequest) -> Future[object]:
        self.calls.append(("delete_alias", request))
        self.aliases = {
            "aliases": [
                entry
                for entry in self.aliases["aliases"]
                if entry["canonical_name"] != request.canonical_name
            ]
        }
        self.aliases["count"] = len(self.aliases["aliases"])
        return self._completed({"updated": False, "deleted": True, "entry": None})


class ParseTagTextTest(unittest.TestCase):
    def test_editor_accepts_common_separators_and_deduplicates(self) -> None:
        self.assertEqual(
            parse_tag_text(" 站姿,微笑；站姿 | 原神 "),
            ("站姿", "微笑", "原神"),
        )


@unittest.skipUnless(os.name == "nt", "tkinter UI tests require Windows")
class OrganizePanelUiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name)
        self.image_path = self.root_path / "preview.png"
        Image.new("RGB", (80, 120), (110, 80, 180)).save(self.image_path)
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors: list[tuple[str, str, int]] = []
        self.confirmations: list[tuple[str, str]] = []
        self.operations = FakeOrganizeOperations(self.image_path)
        self.dispatcher = ImageTaskDispatcher(
            self.root,
            lambda _path, size, _mode: Image.new("RGB", size, (60, 70, 90)),
            workers=1,
        )
        self.panel = OrganizePanel(
            self.root,
            library_id="library-a",
            operations=self.operations,
            image_dispatcher=self.dispatcher,
            auto_load=False,
            confirm=self._confirm,
            report_error=self._report_error,
        )
        self.panel.pack(fill="both", expand=True)
        self.root.update()

    def tearDown(self) -> None:
        with self.subTest("panel cleanup"):
            with suppress(tk.TclError):
                self.panel.destroy()
            self.dispatcher.close()
            with suppress(tk.TclError):
                self.root.destroy()

    def _confirm(self, title: str, message: str) -> bool:
        self.confirmations.append((title, message))
        return True

    def _report_error(self, title: str, message: str) -> None:
        self.errors.append((title, message, threading.get_ident()))

    def _pump_until(self, predicate: Any, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not reached before timeout")

    def _load(self) -> None:
        self.panel.refresh()
        self._pump_until(lambda: self.panel.page is not None)

    def test_load_renders_review_rows_thumbnail_sources_and_difference(self) -> None:
        self._load()

        self.assertEqual(len(self.panel._review_tree.get_children()), 2)
        self.assertEqual(self.panel.selected_proposal_id, "proposal-000")
        self.assertEqual(self.panel._preview.mode, "contain")
        self.assertEqual(self.panel._preview.path, self.image_path)
        self.assertEqual(len(self.panel._source_tree.get_children()), 4)
        self.assertIn("低风险新增 站姿", self.panel._difference_var.get())
        self.assertIn("身份待确认 刻晴 · 原神", self.panel._difference_var.get())
        self.assertEqual(set(self.panel._identity_variables), {"刻晴", "原神"})
        self.assertEqual(self.errors, [])

    def test_filters_and_pagination_build_library_task_requests(self) -> None:
        self.operations.pending_count = 201
        self._load()
        self.panel._latest_var.set(True)
        self.panel._character_var.set("刻")
        self.panel._work_var.set("神")
        self.panel._action_var.set("站")
        self.panel._expression_var.set("笑")
        self.panel._review_state_var.set("身份待确认")

        self.panel.apply_filters()
        self._pump_until(lambda: len(self.operations.calls) >= 2)
        _name, request = self.operations.calls[-1]
        assert isinstance(request, AutoTagPendingRequest)
        self.assertEqual(
            request.filters.to_params(),
            {
                "latest_index_only": True,
                "character": "刻",
                "work": "神",
                "action": "站",
                "expression": "笑",
                "review_state": "identity",
            },
        )

        self._pump_until(lambda: self.panel.page is not None)
        self.panel.next_page()
        self._pump_until(
            lambda: (
                self.panel.page is not None and self.panel.page.pagination.offset == 100
            )
        )
        assert self.panel.page is not None
        self.assertEqual(self.panel.page.pagination.offset, 100)

    def test_edit_and_identity_confirmation_never_submit_unchecked_identity(
        self,
    ) -> None:
        self._load()
        self.panel._edited_tags_var.set("站姿 刻晴 原神")
        self.panel.confirm_selected_identity("刻晴", True)
        self.panel.mark_selected_edit()
        self.panel.submit_reviews()
        self._pump_until(
            lambda: any(name == "submit_review" for name, _ in self.operations.calls)
        )

        _name, request = next(
            value for value in self.operations.calls if value[0] == "submit_review"
        )
        assert isinstance(request, AutoTagReviewRequest)
        decision = request.to_params()["decisions"][0]
        self.assertEqual(decision["decision"], "edit")
        self.assertEqual(decision["accepted_tags"], ["站姿", "刻晴"])
        self.assertEqual(decision["confirmed_identity_tags"], ["刻晴"])
        self.assertNotIn("原神", decision["accepted_tags"])

    def test_one_hundred_item_keyboard_batch_path_stays_safe(self) -> None:
        self.operations.page_items = 100
        self.operations.pending_count = 100
        self._load()

        started = time.perf_counter()
        self.panel.select_all_low_risk()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 2.0)
        assert self.panel.session is not None
        self.assertEqual(self.panel.session.progress.reviewed_items, 100)
        self.assertEqual(self.panel.session.progress.batch_selected_items, 100)
        self.assertTrue(self.panel.session.progress.target_reached)

        self.panel.submit_low_risk_batch()
        self._pump_until(
            lambda: any(
                name == "submit_low_risk_batch" for name, _ in self.operations.calls
            )
        )
        request = next(
            value
            for name, value in self.operations.calls
            if name == "submit_low_risk_batch"
        )
        assert isinstance(request, LowRiskBatchReviewRequest)
        params = request.to_params()
        self.assertEqual(len(params["proposal_ids"]), 100)
        self.assertTrue(params["exclude_identity_tags"])
        self.assertTrue(
            all(
                tags == ["站姿"]
                for tags in params["accepted_tags_by_proposal"].values()
            )
        )

        self.assertTrue(self.panel._review_tree.bind("<KeyPress-a>"))
        self.assertTrue(self.panel._review_tree.bind("<Control-Return>"))

    def test_identity_can_be_submitted_for_only_the_selected_image(self) -> None:
        self._load()
        self.panel.confirm_selected_identity("刻晴", True)
        self.panel.submit_selected_identity()
        self._pump_until(
            lambda: any(
                name == "submit_identity_confirmation"
                for name, _ in self.operations.calls
            )
        )
        request = next(
            value
            for name, value in self.operations.calls
            if name == "submit_identity_confirmation"
        )
        assert isinstance(request, IdentityConfirmationRequest)
        params = request.to_params()
        self.assertEqual(len(params["decisions"]), 1)
        self.assertEqual(params["decisions"][0]["confirmed_identity_tags"], ["刻晴"])

    def test_undo_and_alias_crud_use_injected_async_operations(self) -> None:
        self._load()
        self.panel.undo_last_batch()
        self._pump_until(
            lambda: any(
                name == "undo_low_risk_batch" for name, _ in self.operations.calls
            )
        )
        undo_request = next(
            value
            for name, value in self.operations.calls
            if name == "undo_low_risk_batch"
        )
        self.assertIsInstance(undo_request, AutoTagUndoRequest)

        self.panel.refresh_aliases()
        self._pump_until(lambda: self.panel.alias_catalog.count == 2)
        self.assertEqual(len(self.panel._alias_tree.get_children()), 2)
        self.panel._alias_canonical_var.set("雷电将军")
        self.panel._alias_values_var.set("雷神 影 巴尔泽布")
        self.panel.save_alias()
        self._pump_until(
            lambda: any(name == "upsert_alias" for name, _ in self.operations.calls)
        )
        upsert = next(
            value for name, value in self.operations.calls if name == "upsert_alias"
        )
        assert isinstance(upsert, TagAliasUpsertRequest)
        self.assertEqual(upsert.aliases, ("雷神", "影", "巴尔泽布"))

        self.panel.delete_alias()
        self._pump_until(
            lambda: any(name == "delete_alias" for name, _ in self.operations.calls)
        )
        deletion = next(
            value for name, value in self.operations.calls if name == "delete_alias"
        )
        self.assertIsInstance(deletion, TagAliasDeleteRequest)

    def test_worker_completion_updates_tk_only_on_creating_thread(self) -> None:
        self.operations.use_deferred_pending = True
        self.panel.refresh()
        self.assertIsNotNone(self.operations.deferred_pending)
        future = self.operations.deferred_pending
        assert future is not None
        request = self.operations.calls[-1][1]
        assert isinstance(request, AutoTagPendingRequest)
        worker_id: list[int] = []

        def complete() -> None:
            worker_id.append(threading.get_ident())
            future.set_result(self.operations._pending_payload(request))

        thread = threading.Thread(target=complete)
        thread.start()
        thread.join(timeout=5)
        self._pump_until(lambda: self.panel.page is not None)

        self.assertNotEqual(worker_id[0], threading.get_ident())
        self.assertEqual(
            self.panel.last_ui_update_thread_id,
            threading.get_ident(),
        )
        self.assertEqual(self.errors, [])

    def test_worker_failure_is_reported_on_tk_thread(self) -> None:
        self.operations.use_deferred_pending = True
        self.panel.refresh()
        future = self.operations.deferred_pending
        assert future is not None

        thread = threading.Thread(
            target=lambda: future.set_exception(RuntimeError("backend offline"))
        )
        thread.start()
        thread.join(timeout=5)
        self._pump_until(lambda: bool(self.errors))

        self.assertIn("backend offline", self.errors[0][1])
        self.assertEqual(self.errors[0][2], threading.get_ident())

    def test_public_ui_mutations_reject_worker_thread_calls(self) -> None:
        self._load()
        failures: list[BaseException] = []

        def mutate() -> None:
            try:
                self.panel.select_all_low_risk()
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=mutate)
        thread.start()
        thread.join(timeout=5)

        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], RuntimeError)
        assert self.panel.session is not None
        self.assertEqual(self.panel.session.progress.batch_selected_items, 0)

    def test_refresh_can_preserve_unsaved_page_when_user_cancels(self) -> None:
        self._load()
        self.panel.mark_selected_reject()
        assert self.panel.session is not None
        self.assertTrue(self.panel.session.has_unsaved_changes)
        call_count = len(self.operations.calls)
        self.panel._confirm = lambda _title, _message: False

        self.panel.refresh()
        self.root.update()

        self.assertEqual(len(self.operations.calls), call_count)
        self.assertTrue(self.panel.session.has_unsaved_changes)

    def test_one_corrupt_item_is_isolated_while_valid_row_remains(self) -> None:
        request = AutoTagPendingRequest("library-a", limit=100)
        payload = self.operations._pending_payload(request)
        damaged = _proposal("damaged", self.image_path)
        damaged["identity_tags"] = "刻晴"
        payload["proposals"].append(damaged)
        payload["pending_count"] += 1
        payload["total_count"] += 1
        payload["has_more"] = False
        self.operations.use_deferred_pending = True

        self.panel.refresh()
        future = self.operations.deferred_pending
        assert future is not None
        future.set_result(payload)
        self._pump_until(lambda: self.panel.page is not None)

        assert self.panel.page is not None
        self.assertEqual(self.panel.page.skipped_count, 1)
        self.assertEqual(len(self.panel._review_tree.get_children()), 2)
        self.assertIn("已隔离 1 条损坏建议", self.panel._issue_var.get())
        self.assertEqual(self.errors, [])


@unittest.skipUnless(os.name == "nt", "tkinter window smoke requires Windows")
class OrganizePanelWindowSmokeTest(unittest.TestCase):
    def test_panel_can_be_embedded_in_notebook_and_render_a_real_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image_path = Path(temporary) / "preview.png"
            Image.new("RGB", (120, 80), (90, 130, 170)).save(image_path)
            root = tk.Tk()
            root.geometry("1280x820+20+20")
            operations = FakeOrganizeOperations(image_path)
            dispatcher = ImageTaskDispatcher(
                root,
                lambda _path, size, _mode: Image.new("RGB", size, (80, 90, 110)),
                workers=1,
            )
            notebook = ttk.Notebook(root)
            notebook.pack(fill="both", expand=True)
            host = ttk.Frame(notebook)
            notebook.add(host, text="智能整理")
            panel = OrganizePanel(
                host,
                library_id="library-a",
                operations=operations,
                image_dispatcher=dispatcher,
                confirm=lambda _title, _message: True,
                report_error=lambda _title, message: self.fail(message),
            )
            panel.pack(fill="both", expand=True)
            try:
                deadline = time.monotonic() + 5
                while panel.page is None and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                self.assertIsNotNone(panel.page)
                self.assertEqual(len(panel._notebook.tabs()), 2)
                self.assertEqual(panel._preview.mode, "contain")
                self.assertGreater(panel.winfo_width(), 800)
                self.assertGreater(panel.winfo_height(), 600)
            finally:
                panel.destroy()
                dispatcher.close()
                root.destroy()


if __name__ == "__main__":
    unittest.main()
