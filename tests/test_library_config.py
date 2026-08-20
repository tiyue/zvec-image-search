from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from image_vector_service.config import ConfigurationError, ServiceConfig
from image_vector_service.library_config import load_library_catalog


class LibraryCatalogTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="zvec_library_config_test_"))
        self.config = ServiceConfig(
            workspace=self.root / "legacy-workspace",
            results_directory=self.root / "results",
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_manifest(self, payload: dict) -> Path:
        path = self.root / "libraries.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def valid_payload(self) -> dict:
        return {
            "schema_version": 2,
            "default_library_id": "photos-a",
            "results_directory": str(self.root / "shared-results"),
            "libraries": [
                {
                    "id": "photos-a",
                    "name": "Photos A",
                    "image_root": str(self.root / "images-a"),
                    "workspace": str(self.root / "workspace-a"),
                    "enabled": True,
                },
                {
                    "id": "photos-b",
                    "name": "Photos B",
                    "image_root": str(self.root / "images-b"),
                    "workspace": str(self.root / "workspace-b"),
                    "enabled": False,
                },
            ],
        }

    def test_missing_manifest_keeps_legacy_default_library(self):
        catalog = load_library_catalog(None, self.config)
        self.assertEqual(catalog.default_library_id, "default")
        self.assertEqual(len(catalog.libraries), 1)
        self.assertEqual(catalog.default.workspace, self.config.workspace)
        self.assertIsNone(catalog.default.image_root)
        self.assertEqual(
            catalog.federated_results_directory,
            self.config.results_path,
        )

    def test_schema_v2_uses_stable_ids_and_container_paths(self):
        catalog = load_library_catalog(
            self.write_manifest(self.valid_payload()), self.config
        )
        self.assertEqual(catalog.default_library_id, "photos-a")
        self.assertEqual([item.library_id for item in catalog.enabled], ["photos-a"])
        self.assertEqual(catalog.by_id["photos-a"].name, "Photos A")
        self.assertEqual(
            catalog.by_id["photos-a"].workspace,
            (self.root / "workspace-a").resolve(),
        )
        self.assertEqual(
            catalog.federated_results_directory,
            (self.root / "shared-results").resolve(),
        )

    def test_schema_v3_prefers_workspace_directory(self):
        payload = self.valid_payload()
        payload["schema_version"] = 3
        for library in payload["libraries"]:
            library["workspace_directory"] = library.pop("workspace")
        catalog = load_library_catalog(self.write_manifest(payload), self.config)
        self.assertEqual(
            catalog.by_id["photos-a"].workspace,
            (self.root / "workspace-a").resolve(),
        )
        serialized = catalog.to_dict()
        self.assertEqual(serialized["schema_version"], 3)
        self.assertIn("workspace_directory", serialized["libraries"][0])
        self.assertNotIn("workspace", serialized["libraries"][0])

    def test_duplicate_missing_and_disabled_default_are_rejected(self):
        duplicate = self.valid_payload()
        duplicate["libraries"][1]["id"] = "photos-a"
        with self.assertRaisesRegex(ConfigurationError, "Duplicate library id"):
            load_library_catalog(self.write_manifest(duplicate), self.config)

        missing = self.valid_payload()
        missing["default_library_id"] = "missing"
        with self.assertRaisesRegex(ConfigurationError, "does not identify"):
            load_library_catalog(self.write_manifest(missing), self.config)

        disabled = self.valid_payload()
        disabled["libraries"][0]["enabled"] = False
        with self.assertRaisesRegex(ConfigurationError, "default library"):
            load_library_catalog(self.write_manifest(disabled), self.config)

    def test_relative_runtime_paths_are_rejected(self):
        payload = self.valid_payload()
        payload["libraries"][0]["workspace"] = "relative/workspace"
        with self.assertRaisesRegex(ConfigurationError, "absolute path"):
            load_library_catalog(self.write_manifest(payload), self.config)


if __name__ == "__main__":
    unittest.main()
