from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zvec_desktop.library_task_ui_state import (
    LibraryTaskFormValues,
    TaskCenterModel,
    build_library_task_request,
    parse_manual_tags,
    project_task,
    resolve_failure_directory,
    task_result_text,
)
from zvec_desktop.library_tasks import (
    IndexAndAutoTagRequest,
    IndexRequest,
    LibraryTaskValidationError,
    RootsRequest,
    StatsRequest,
    SyncRequest,
)


class LibraryTaskFormTest(unittest.TestCase):
    def test_builds_all_first_page_actions_with_new_image_tags(self) -> None:
        values = LibraryTaskFormValues(
            library_id="people",
            manual_tags="原神，写真\n站姿, 原神",
            recursive=False,
            verify_hash=True,
        )
        index = build_library_task_request("index", values)
        self.assertIsInstance(index, IndexRequest)
        self.assertEqual(index.tags, ("原神", "写真", "站姿"))
        self.assertFalse(index.recursive)
        self.assertTrue(index.verify_hash)

        sync = build_library_task_request("sync", values)
        self.assertIsInstance(sync, SyncRequest)
        self.assertNotIn("tags", sync.to_params())
        self.assertIsInstance(build_library_task_request("stats", values), StatsRequest)
        self.assertIsInstance(build_library_task_request("roots", values), RootsRequest)

    def test_combined_task_validates_model_limits_budget_and_confirmation(self) -> None:
        with self.assertRaisesRegex(
            LibraryTaskValidationError,
            "[Ee]xternal processing",
        ):
            build_library_task_request(
                "index_and_auto_tag",
                LibraryTaskFormValues(library_id="people"),
            )

        request = build_library_task_request(
            "index_and_auto_tag",
            LibraryTaskFormValues(
                library_id="people",
                manual_tags="本次新增",
                model="qwen3-vl-flash",
                max_images="120",
                max_budget_cny="2.5",
                external_processing_confirmed=True,
            ),
        )
        self.assertIsInstance(request, IndexAndAutoTagRequest)
        self.assertEqual(request.tags, ("本次新增",))
        self.assertEqual(request.max_images, 120)
        self.assertEqual(request.max_budget_cny, 2.5)

        for values in (
            LibraryTaskFormValues(library_id="people", max_images="zero"),
            LibraryTaskFormValues(library_id="people", max_budget_cny="0"),
            LibraryTaskFormValues(library_id=""),
        ):
            with (
                self.subTest(values=values),
                self.assertRaises(LibraryTaskValidationError),
            ):
                build_library_task_request("index_and_auto_tag", values)

    def test_tag_parser_deduplicates_without_splitting_words(self) -> None:
        self.assertEqual(
            parse_manual_tags("雷电将军, 雷神；影、雷电将军"),
            ("雷电将军", "雷神", "影"),
        )


class TaskCenterStateTest(unittest.TestCase):
    def test_terminal_task_cannot_regress_and_later_failure_does_not_break_model(
        self,
    ) -> None:
        model = TaskCenterModel(capacity=3)
        succeeded = {
            "id": "job-1",
            "command": "index",
            "status": "succeeded",
            "result": {"inserted": 10, "failed": 0},
        }
        self.assertTrue(model.update(succeeded))
        self.assertFalse(
            model.update(
                {
                    "id": "job-1",
                    "command": "index",
                    "status": "running",
                }
            )
        )
        self.assertEqual(model.get("job-1")["status"], "succeeded")  # type: ignore[index]

        failed = {
            "id": "job-2",
            "command": "sync",
            "status": "failed",
            "error": {"message": "one task failed"},
        }
        self.assertTrue(model.update(failed))
        self.assertEqual(len(model.jobs), 2)
        self.assertEqual(project_task(failed).message, "one task failed")
        self.assertIn("one task failed", task_result_text(failed))

    def test_projection_includes_progress_failures_and_cancel_state(self) -> None:
        view = project_task(
            {
                "id": "job-1",
                "command": "index_and_auto_tag",
                "params": {"library_id": "people"},
                "status": "running",
                "progress": {
                    "message": "正在处理",
                    "current": 20,
                    "total": 100,
                    "failed": 3,
                },
                "failure_count": 2,
            }
        )
        self.assertEqual(view.title, "索引并自动标注")
        self.assertEqual(view.library, "people")
        self.assertEqual(view.progress_text, "20/100 · 失败 3")
        self.assertTrue(view.cancellable)

    def test_failure_directory_must_be_owned_and_contain_existing_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            owned = root / "failed-images"
            owned.mkdir()
            manifest = owned / "run-1" / "failures.json"
            manifest.parent.mkdir()
            manifest.write_text("{}", encoding="utf-8")
            job = {
                "id": "job-1",
                "status": "partial",
                "result": {
                    "quarantined": 2,
                    "failure_manifest": str(manifest),
                },
            }
            self.assertEqual(resolve_failure_directory(root, job), owned)

            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            job["result"]["failure_manifest"] = str(outside)  # type: ignore[index]
            self.assertIsNone(resolve_failure_directory(root, job))


if __name__ == "__main__":
    unittest.main()
