from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from image_vector_service.backend_server import BackendRequestError, _normalize_job
from zvec_host.library_tasks import (
    FolderNameTagApplyRequest,
    FolderNameTagEstimateRequest,
    FolderNameTagSelection,
    LibraryTaskValidationError,
)
from zvec_webview.facade import _library_request


class FolderNameTagTaskContractTest(unittest.TestCase):
    def test_host_requests_preserve_library_and_folder_scopes(self) -> None:
        estimate = FolderNameTagEstimateRequest(
            "library-a",
            FolderNameTagSelection("library"),
        )
        self.assertEqual(
            estimate.to_params(),
            {
                "library_id": "library-a",
                "selection": {"mode": "library"},
            },
        )
        apply = FolderNameTagApplyRequest(
            "library-a",
            FolderNameTagSelection(
                "folder",
                folder_key="folder-key",
                include_subfolders=True,
            ),
        )
        self.assertEqual(
            apply.to_params()["selection"],
            {
                "mode": "folder",
                "folder_key": "folder-key",
                "include_subfolders": True,
            },
        )
        self.assertTrue(
            FolderNameTagSelection(
                "folder",
                folder_key="folder-key",
            ).include_subfolders
        )

    def test_invalid_scope_is_rejected_at_host_and_backend_boundaries(self) -> None:
        with self.assertRaises(LibraryTaskValidationError):
            FolderNameTagSelection("folder")
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaises(BackendRequestError),
        ):
            _normalize_job(
                {
                    "command": "folder_name_tag_apply",
                    "params": {
                        "library_id": "library-a",
                        "selection": {
                            "mode": "selected",
                            "doc_ids": ["doc-a"],
                        },
                    },
                },
                Path(temporary),
            )

    def test_webview_parser_builds_the_typed_apply_request(self) -> None:
        task_type, request, library_id = _library_request(
            {
                "task_type": "folder_name_tag_apply",
                "library_id": "library-a",
                "selection": {
                    "mode": "folder",
                    "folder_key": "folder-key",
                    "include_subfolders": True,
                },
            }
        )
        self.assertEqual(task_type, "folder_name_tag_apply")
        self.assertEqual(library_id, "library-a")
        self.assertIsInstance(request, FolderNameTagApplyRequest)
        self.assertEqual(request.to_params()["selection"]["mode"], "folder")


if __name__ == "__main__":
    unittest.main()
