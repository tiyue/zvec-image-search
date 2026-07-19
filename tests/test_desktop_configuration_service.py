from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zvec_desktop.configuration_service import (
    DesktopConfigurationError,
    DesktopConfigurationService,
)


class DesktopConfigurationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config_home = self.root / "config-home"
        self.config_path = self.config_home / "config.json"
        self.images = self.root / "images"
        self.images.mkdir()
        self.service = DesktopConfigurationService(
            self.config_path, config_home=self.config_home
        )

    def test_create_initial_builds_schema_v3_and_models_json(self) -> None:
        snapshot = self.service.create_initial(self.images, name="人物图库")

        self.assertEqual(snapshot.path, self.config_path.absolute())
        self.assertEqual(snapshot.configuration.libraries[0].name, "人物图库")
        self.assertEqual(snapshot.configuration.libraries[0].image_root, self.images)
        self.assertTrue(
            snapshot.configuration.libraries[0].workspace_directory.is_dir()
        )
        self.assertTrue(snapshot.configuration.results_directory.is_dir())
        self.assertTrue((self.config_home / "models.json").is_file())
        payload = json.loads(self.config_path.read_text("utf-8"))
        self.assertEqual(payload["schema_version"], 3)
        self.assertNotIn("image_name", payload)
        self.assertNotIn("workspace_type", payload)

    def test_create_initial_refuses_to_replace_existing_config(self) -> None:
        self.service.create_initial(self.images)
        with self.assertRaisesRegex(DesktopConfigurationError, "已存在"):
            self.service.create_initial(self.images)

    def test_add_library_preserves_existing_and_uses_separate_workspace(self) -> None:
        first = self.service.create_initial(self.images)
        second_images = self.root / "cosplay"
        second_images.mkdir()

        changed = self.service.add_library(second_images, name="Cosplay")

        self.assertEqual(len(changed.libraries), 2)
        self.assertEqual(
            changed.configuration.default_library_id,
            first.libraries[0].library_id,
        )
        self.assertNotEqual(
            changed.libraries[0].workspace_directory,
            changed.libraries[1].workspace_directory,
        )

    def test_overlap_validation_prevents_results_inside_image_root(self) -> None:
        with self.assertRaisesRegex(DesktopConfigurationError, "overlap"):
            self.service.create_initial(
                self.images,
                results_directory=self.images / "results",
            )

    def test_disable_default_or_last_library_is_rejected(self) -> None:
        snapshot = self.service.create_initial(self.images)
        library_id = snapshot.configuration.default_library_id
        with self.assertRaises(DesktopConfigurationError):
            self.service.set_library_enabled(library_id, False)

    def test_set_default_library_requires_enabled_existing_library(self) -> None:
        first = self.service.create_initial(self.images)
        second_images = self.root / "anime"
        second_images.mkdir()
        changed = self.service.add_library(second_images, name="动漫")
        second_id = changed.libraries[1].library_id

        selected = self.service.set_default_library(second_id)

        self.assertEqual(selected.configuration.default_library_id, second_id)
        self.assertNotEqual(first.configuration.default_library_id, second_id)
        with self.assertRaises(DesktopConfigurationError):
            self.service.set_default_library("missing")

    def test_update_library_preserves_id_and_updates_global_results(self) -> None:
        first = self.service.create_initial(self.images, name="People")
        library_id = first.configuration.default_library_id
        replacement_images = self.root / "replacement-images"
        replacement_images.mkdir()
        replacement_workspace = self.root / "replacement-workspace"
        replacement_results = self.root / "replacement-results"

        changed = self.service.update_library(
            library_id,
            name="Cosplay portraits",
            image_root=replacement_images,
            workspace_directory=replacement_workspace,
            enabled=True,
            is_default=True,
            results_directory=replacement_results,
        )

        library = changed.configuration.by_id[library_id]
        self.assertEqual(library.library_id, library_id)
        self.assertEqual(library.name, "Cosplay portraits")
        self.assertEqual(library.image_root, replacement_images.resolve())
        self.assertEqual(library.workspace_directory, replacement_workspace.resolve())
        self.assertEqual(
            changed.configuration.results_directory, replacement_results.resolve()
        )
        self.assertTrue(replacement_workspace.is_dir())
        self.assertTrue(replacement_results.is_dir())

    def test_update_can_enable_and_select_an_existing_library(self) -> None:
        first = self.service.create_initial(self.images)
        second_images = self.root / "second-images"
        second_images.mkdir()
        added = self.service.add_library(second_images, enabled=False)
        second_id = added.libraries[1].library_id

        changed = self.service.update_library(second_id, enabled=True, is_default=True)

        self.assertEqual(changed.configuration.default_library_id, second_id)
        self.assertTrue(changed.configuration.by_id[second_id].enabled)
        self.assertNotEqual(first.configuration.default_library_id, second_id)

    def test_update_enforces_default_library_rules_without_mutation(self) -> None:
        first = self.service.create_initial(self.images)
        first_id = first.configuration.default_library_id
        second_images = self.root / "disabled-images"
        second_images.mkdir()
        added = self.service.add_library(second_images, enabled=False)
        second_id = added.libraries[1].library_id
        before = self.config_path.read_bytes()

        with self.assertRaisesRegex(DesktopConfigurationError, "default"):
            self.service.update_library(first_id, is_default=False)
        self.assertEqual(self.config_path.read_bytes(), before)

        with self.assertRaisesRegex(DesktopConfigurationError, "default"):
            self.service.update_library(second_id, is_default=True)
        self.assertEqual(self.config_path.read_bytes(), before)

        with self.assertRaisesRegex(DesktopConfigurationError, "default"):
            self.service.update_library(first_id, enabled=False)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_update_rejects_relative_or_overlapping_paths_without_mutation(
        self,
    ) -> None:
        snapshot = self.service.create_initial(self.images)
        library_id = snapshot.configuration.default_library_id
        before = self.config_path.read_bytes()

        with self.assertRaisesRegex(DesktopConfigurationError, "absolute Windows"):
            self.service.update_library(library_id, workspace_directory="relative")
        self.assertEqual(self.config_path.read_bytes(), before)

        nested_workspace = self.images / "unsafe-workspace"
        with self.assertRaisesRegex(DesktopConfigurationError, "overlap"):
            self.service.update_library(
                library_id, workspace_directory=nested_workspace
            )
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(nested_workspace.exists())

        nested_results = self.images / "unsafe-results"
        with self.assertRaisesRegex(DesktopConfigurationError, "overlap"):
            self.service.update_library(library_id, results_directory=nested_results)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(nested_results.exists())

    def test_update_write_failure_keeps_previous_config(self) -> None:
        snapshot = self.service.create_initial(self.images, name="Original")
        library_id = snapshot.configuration.default_library_id
        original_results = snapshot.configuration.results_directory
        failed_results = self.root / "failed-results"
        before = self.config_path.read_bytes()

        with (
            patch.object(Path, "replace", side_effect=OSError("locked")),
            self.assertRaises(DesktopConfigurationError),
        ):
            self.service.update_library(
                library_id,
                name="Replacement",
                results_directory=failed_results,
            )

        self.assertEqual(self.config_path.read_bytes(), before)
        loaded = self.service.load()
        assert loaded is not None
        self.assertEqual(loaded.configuration.by_id[library_id].name, "Original")
        self.assertEqual(loaded.configuration.results_directory, original_results)

    def test_load_rejects_legacy_config_without_mutating_it(self) -> None:
        self.config_home.mkdir(parents=True)
        original = '{"schema_version":2,"image_root":"legacy"}\n'
        self.config_path.write_text(original, "utf-8")
        with self.assertRaisesRegex(DesktopConfigurationError, "旧版配置"):
            self.service.load()
        self.assertEqual(self.config_path.read_text("utf-8"), original)

    @unittest.skipUnless(os.name != "nt", "portable symlink contract")
    def test_config_symlink_is_rejected(self) -> None:
        target = self.root / "real.json"
        target.write_text("{}", "utf-8")
        self.config_home.mkdir(parents=True)
        self.config_path.symlink_to(target)
        with self.assertRaisesRegex(DesktopConfigurationError, "链接"):
            self.service.load()


if __name__ == "__main__":
    unittest.main()
