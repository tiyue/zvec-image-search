from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_vector_service.config import ServiceConfig


class ServiceConfigTests(unittest.TestCase):
    def test_results_path_defaults_to_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory).resolve()
            with patch.dict(os.environ, {}, clear=True):
                config = ServiceConfig(workspace=workspace)

            self.assertEqual(config.results_path, workspace / "search_results")

    def test_results_path_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            workspace = root / "workspace"
            results = root / "host-results"
            with patch.dict(
                os.environ,
                {"ZVEC_IMAGE_RESULTS_DIR": str(results)},
                clear=True,
            ):
                config = ServiceConfig(workspace=workspace)
                configured_results = config.results_path

            self.assertEqual(configured_results, results)


if __name__ == "__main__":
    unittest.main()
