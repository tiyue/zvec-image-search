from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from image_vector_service.config import (
    ConfigurationError,
    RuntimeCredentials,
    ServiceConfig,
)
from image_vector_service.dashscope_client import DashScopeEmbeddingClient


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

    def test_explicit_results_and_runtime_credentials_override_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            credentials = RuntimeCredentials()
            credentials.configure("memory-key", "http://127.0.0.1:9000/embed")
            with patch.dict(
                os.environ,
                {
                    "ZVEC_IMAGE_RESULTS_DIR": str(root / "environment-results"),
                    "DASHSCOPE_API_KEY": "environment-key",
                    "DASHSCOPE_API_URL": "https://environment.invalid",
                },
                clear=True,
            ):
                config = ServiceConfig(
                    workspace=root / "workspace",
                    results_directory=root / "explicit-results",
                    runtime_credentials=credentials,
                )
                self.assertEqual(config.results_path, root / "explicit-results")
                self.assertEqual(config.api_key, "memory-key")
                self.assertEqual(config.api_url, "http://127.0.0.1:9000/embed")

    def test_embedding_client_can_start_without_key_but_first_embedding_cannot(
        self,
    ) -> None:
        credentials = RuntimeCredentials()
        with patch.dict(os.environ, {}, clear=True):
            client = DashScopeEmbeddingClient(
                ServiceConfig(runtime_credentials=credentials)
            )
            with self.assertRaises(ConfigurationError):
                client.embed_text("query")


if __name__ == "__main__":
    unittest.main()
