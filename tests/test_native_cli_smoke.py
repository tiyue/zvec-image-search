from __future__ import annotations

import gc
import json
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import zvec_launcher
from tests import mock_dashscope_server


class NativeCliSmokeTest(unittest.TestCase):
    """Exercise the real native CLI path without contacting DashScope."""

    def test_init_index_text_search_and_stats(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="zvec_native_cli_smoke_"))
        images = root / "images"
        workspace = root / "workspace"
        results = root / "results"
        config_home = root / "config"
        images.mkdir()
        Image.new("RGB", (24, 24), (220, 40, 40)).save(images / "portrait.png")

        server = mock_dashscope_server.ThreadingHTTPServer(
            ("127.0.0.1", 0), mock_dashscope_server.Handler
        )
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        api_url = f"http://127.0.0.1:{server.server_address[1]}/embeddings"

        environment = {
            "ZVEC_CONFIG_HOME": str(config_home),
            "DASHSCOPE_API_KEY": "test-key-not-real",
            "DASHSCOPE_API_URL": api_url,
            "ZVEC_NO_OPEN": "1",
        }
        try:
            with mock_dashscope_server.COUNTER_LOCK:
                mock_dashscope_server.REQUEST_COUNT = 0
                mock_dashscope_server.CONTENT_COUNT = 0
            with patch.dict(os.environ, environment, clear=False):
                os.environ.pop("ZVEC_DOCKER_CONFIG_HOME", None)
                os.environ.pop("ZVEC_LIBRARY_ID", None)

                init = self._run(
                    "init",
                    str(images),
                    "--workspace",
                    str(workspace),
                    "--results",
                    str(results),
                    "--skip-key",
                )
                self.assertIn("native Python", init)

                indexed = self._json_result(self._run("index"))
                self.assertEqual(indexed["inserted"], 1)

                searched = self._json_result(
                    self._run(
                        "search",
                        "red portrait",
                        "--tk",
                        "3",
                        "--show-low-confidence",
                    )
                )
                self.assertEqual(searched["query_type"], "text")

                stats = self._json_result(self._run("stats"))
                self.assertEqual(stats["collection_stats"]["doc_count"], 1)
                self.assertEqual(stats["tracked_files"], 1)

            with mock_dashscope_server.COUNTER_LOCK:
                self.assertEqual(mock_dashscope_server.REQUEST_COUNT, 2)
                self.assertEqual(mock_dashscope_server.CONTENT_COUNT, 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            gc.collect()
            shutil.rmtree(root, ignore_errors=True)

    def _run(self, *arguments: str) -> str:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = zvec_launcher.main(list(arguments))
        self.assertEqual(exit_code, 0, stderr.getvalue() or stdout.getvalue())
        return stdout.getvalue()

    @staticmethod
    def _json_result(output: str) -> dict:
        start = output.find("{")
        if start < 0:
            raise AssertionError(f"CLI output did not contain JSON: {output}")
        value = json.loads(output[start:])
        if not isinstance(value, dict):
            raise AssertionError("CLI JSON result must be an object.")
        return value


if __name__ == "__main__":
    unittest.main()
