from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from zvec_desktop.configuration_service import DesktopConfigurationService
from zvec_desktop.credentials import SessionCredentialStore
from zvec_desktop.search_service import SearchOutcome, SubmittedSearch
from zvec_webview.facade import PreviewFacade, _SearchOperation


class _IdleHost:
    is_running = False

    def stop(self, *, force: bool = False) -> None:
        del force


class WebviewSearchLearningIntegrationTests(unittest.TestCase):
    def test_completed_search_records_bounded_private_features_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            images = root / "images"
            workspace = root / "workspace"
            results = root / "results"
            for directory in (images, workspace, results):
                directory.mkdir()
            config_path = root / "config.json"
            snapshot = DesktopConfigurationService(config_path).create_initial(
                images,
                workspace_directory=workspace,
                results_directory=results,
            )
            library_id = snapshot.configuration.default_library_id
            source = images / "character.jpg"
            Image.new("RGB", (60, 90), (120, 70, 150)).save(source)
            output = results / "search-output"
            output.mkdir()
            copied = output / "001.jpg"
            Image.open(source).save(copied)
            manifest_path = output / "results.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "created_at": "2026-07-19T12:00:00Z",
                        "query_type": "text",
                        "status": "ok",
                        "sort_mode": "confidence",
                        "library_ids": [library_id],
                        "ranking_diagnostics": {
                            "ranking_model_version": "ranker-baseline",
                            "calibration_version": "calibration-baseline",
                        },
                        "results": [
                            {
                                "rank": 1,
                                "copied_file": copied.name,
                                "relative_path": source.name,
                                "library_id": library_id,
                                "doc_id": "doc-stable-1",
                                "sha256": "a" * 64,
                                "raw_score": 0.18,
                                "normalized_score": 0.91,
                                "confidence": 0.91,
                                "ranking_confidence": 0.93,
                                "metadata_confidence": 0.72,
                                "rank_agreement": 0.8,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            facade = PreviewFacade(
                config_path,
                backend_host=_IdleHost(),  # type: ignore[arg-type]
                credential_store=SessionCredentialStore(),
            )
            submission = SubmittedSearch(
                job_id="search-stable-1",
                query_type="text",
                staged_image=None,
                submitted_job={},
            )
            outcome = SearchOutcome(
                submission=submission,
                status="succeeded",
                job={},
                result={"result_count": 1},
                error=None,
                output_directory=output,
                manifest_path=manifest_path,
            )
            operation = _SearchOperation(
                operation_id="search-stable-1",
                query={
                    "text": "private character query",
                    "mode": "semantic",
                    "top_k": 15,
                    "library_ids": [library_id],
                },
                submitted_at=time.time() - 0.05,
                submission=submission,
                status="succeeded",
                outcome=outcome,
                finished_at=time.time(),
            )
            try:
                facade._record_search_learning_session(operation)  # noqa: SLF001
                sessions = facade.search_learning_sessions()
                self.assertEqual(len(sessions["items"]), 1)
                self.assertEqual(sessions["items"][0]["session_id"], "search-stable-1")
                self.assertEqual(sessions["items"][0]["candidate_count"], 1)

                connection = sqlite3.connect(root / "search-learning.sqlite3")
                try:
                    query_text = connection.execute(
                        "SELECT query_text FROM search_sessions"
                    ).fetchone()[0]
                    candidate = connection.execute(
                        "SELECT doc_id, sha256, features_json FROM search_candidates"
                    ).fetchone()
                finally:
                    connection.close()
                self.assertIsNone(query_text)
                self.assertEqual(candidate[0], "doc-stable-1")
                self.assertEqual(candidate[1], "a" * 64)
                self.assertNotIn(str(root), candidate[2])

                page = facade._catalog().load_manifest(  # noqa: SLF001
                    manifest_path, page=1, page_size=15
                )
                response = facade._page_response(  # noqa: SLF001
                    page,
                    operation_id="search-stable-1",
                    query={},
                    status="succeeded",
                    elapsed_ms=50,
                )
                item = response["items"][0]
                self.assertEqual(item["search_session_id"], "search-stable-1")
                self.assertEqual(item["doc_id"], "doc-stable-1")
                self.assertEqual(item["sha256"], "a" * 64)
            finally:
                facade.close(force=True)


if __name__ == "__main__":
    unittest.main()
