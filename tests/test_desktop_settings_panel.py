from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zvec_desktop.configuration_service import (
    DesktopConfigurationError,
    DesktopConfigurationService,
)
from zvec_desktop.credentials import SessionCredentialStore
from zvec_desktop.model_settings import ModelSettingsError, ModelSettingsService
from zvec_desktop.settings_panel import (
    SettingsController,
    SettingsPanel,
    SettingsPanelError,
    SettingsWindow,
)


class _StatusOnlyCredentialStore:
    def __init__(self) -> None:
        self.secret: str | None = None
        self.read_calls = 0

    @property
    def persistent(self) -> bool:
        return True

    def has_secret(self) -> bool:
        return self.secret is not None

    def read_secret(self) -> str | None:
        self.read_calls += 1
        raise AssertionError("The settings surface must not read or reveal the secret")

    def save_secret(self, secret: str) -> None:
        if not secret.strip():
            raise ValueError("empty")
        self.secret = secret.strip()

    def delete_secret(self) -> None:
        self.secret = None


class SettingsControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_home = self.root / "config-home"
        self.config_service = DesktopConfigurationService(config_home=self.config_home)
        self.model_service = ModelSettingsService(self.config_home / "models.json")
        self.credentials = _StatusOnlyCredentialStore()
        self.opened: list[Path] = []
        self.controller = SettingsController(
            self.config_service,
            self.model_service,
            self.credentials,
            path_opener=self.opened.append,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _directory(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _initialize(self) -> tuple[Path, Path, Path]:
        images = self._directory("图片一")
        workspace = self._directory("工作区一")
        results = self._directory("结果")
        state = self.controller.create_initial_library(
            images,
            name="人物图库",
            workspace_directory=workspace,
            results_directory=results,
        )
        self.assertTrue(state.configured)
        self.assertTrue(state.backend_restart_required)
        return images, workspace, results

    def test_first_use_creates_only_config_and_preserves_selected_workspace(
        self,
    ) -> None:
        images = self._directory("图片")
        workspace = self._directory("已有工作区")
        results = self._directory("搜索结果")
        sentinel = workspace / "existing.collection.data"
        sentinel.write_bytes(b"do-not-change")

        initial = self.controller.load()
        self.assertFalse(initial.configured)
        self.assertTrue(self.model_service.path.is_file())

        state = self.controller.create_initial_library(
            images,
            name="Cosplay",
            workspace_directory=workspace,
            results_directory=results,
        )

        self.assertTrue(state.configured)
        assert state.configuration is not None
        self.assertEqual(len(state.configuration.libraries), 1)
        library = state.configuration.libraries[0]
        self.assertEqual(library.image_root, images.resolve())
        self.assertEqual(library.workspace_directory, workspace.resolve())
        self.assertEqual(state.configuration.configuration.results_directory, results)
        self.assertEqual(sentinel.read_bytes(), b"do-not-change")
        config = json.loads(self.config_service.path.read_text(encoding="utf-8"))
        self.assertEqual(config["schema_version"], 3)
        self.assertNotIn("workspace_type", config)
        self.assertNotIn("workspace_source", config)

    def test_first_use_rejects_an_empty_image_root(self) -> None:
        with self.assertRaisesRegex(SettingsPanelError, "图片根目录"):
            self.controller.create_initial_library("")
        self.assertFalse(self.config_service.path.exists())

    def test_add_enable_disable_and_default_library(self) -> None:
        _images, _workspace, _results = self._initialize()
        second_images = self._directory("图片二")
        second_workspace = self._directory("工作区二")
        sentinel = second_workspace / "existing.vector.segment"
        sentinel.write_bytes(b"keep")

        added = self.controller.add_library(
            second_images,
            name="动漫图库",
            workspace_directory=second_workspace,
        )
        assert added.configuration is not None
        self.assertTrue(added.backend_restart_required)
        first, second = added.configuration.libraries
        self.assertEqual(
            added.configuration.configuration.default_library_id, first.library_id
        )

        defaulted = self.controller.set_default_library(second.library_id)
        assert defaulted.configuration is not None
        self.assertTrue(defaulted.backend_restart_required)
        self.assertEqual(
            defaulted.configuration.configuration.default_library_id,
            second.library_id,
        )
        disabled = self.controller.set_library_enabled(first.library_id, False)
        assert disabled.configuration is not None
        self.assertTrue(disabled.backend_restart_required)
        self.assertFalse(disabled.configuration.libraries[0].enabled)
        with self.assertRaises(DesktopConfigurationError):
            self.controller.set_library_enabled(second.library_id, False)
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_model_choices_and_advanced_json_come_only_from_models_file(self) -> None:
        initial = self.controller.load()
        self.assertEqual(
            {choice.model_id for choice in initial.models.embedding_choices},
            {"qwen3-vl-embedding"},
        )
        payload = json.loads(initial.models.json_text)
        payload["models"].append(
            {
                "id": "qwen3-vl-custom",
                "display_name": "Custom Alibaba Visual",
                "roles": ["auto_tag_primary", "auto_tag_escalation"],
                "protocol": "dashscope_multimodal_conversation",
                "enabled": True,
                "pricing": {
                    "input_yuan_per_million": 0.2,
                    "output_yuan_per_million": 1.8,
                    "effective_from": "2026-07-18",
                },
            }
        )
        replaced = self.controller.replace_model_json(
            json.dumps(payload, ensure_ascii=False)
        )
        self.assertTrue(replaced.backend_restart_required)
        self.assertIn(
            "qwen3-vl-custom",
            {choice.model_id for choice in replaced.models.auto_tag_primary_choices},
        )

        assigned = self.controller.assign_model_roles(
            embedding_model="qwen3-vl-embedding",
            auto_tag_primary_model="qwen3-vl-custom",
            auto_tag_escalation_model="qwen3-vl-plus",
        )
        self.assertEqual(assigned.models.auto_tag_primary_model, "qwen3-vl-custom")
        saved = json.loads(self.model_service.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["provider"], "aliyun_dashscope")
        self.assertEqual(saved["roles"]["auto_tag_primary"], "qwen3-vl-custom")

    def test_invalid_advanced_json_is_not_saved_or_marked_for_restart(self) -> None:
        state = self.controller.load()
        before = self.model_service.path.read_bytes()
        invalid = json.loads(state.models.json_text)
        invalid["provider"] = "other_provider"
        with self.assertRaises(ModelSettingsError):
            self.controller.replace_model_json(json.dumps(invalid))
        self.assertEqual(self.model_service.path.read_bytes(), before)
        self.assertFalse(self.controller.load().backend_restart_required)

    def test_credential_status_never_reads_or_returns_full_secret(self) -> None:
        initial = self.controller.load()
        self.assertFalse(initial.credential_saved)
        secret = "sk-sensitive-value-that-must-not-be-rendered"
        saved = self.controller.save_api_key(secret)
        self.assertTrue(saved.credential_saved)
        self.assertTrue(saved.credential_persistent)
        self.assertNotIn(secret, repr(saved))
        self.assertEqual(self.credentials.read_calls, 0)
        deleted = self.controller.delete_api_key()
        self.assertFalse(deleted.credential_saved)
        self.assertTrue(deleted.backend_restart_required)
        self.assertEqual(self.credentials.read_calls, 0)

    def test_config_and_model_files_use_injected_system_opener(self) -> None:
        self.controller.load()
        with self.assertRaises(SettingsPanelError):
            self.controller.open_config_file()
        self.controller.open_models_file()
        self._initialize()
        self.controller.open_config_file()
        self.assertEqual(
            self.opened,
            [self.model_service.path, self.config_service.path],
        )


@unittest.skipUnless(os.name == "nt", "tkinter settings smoke is Windows-only")
class SettingsPanelWindowSmokeTest(unittest.TestCase):
    def test_first_use_models_and_secret_status_render_without_revealing_key(
        self,
    ) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"tkinter is unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_home = root / "config-home"
            images = root / "images"
            workspace = root / "workspace"
            results = root / "results"
            for directory in (images, workspace, results):
                directory.mkdir(parents=True)
            config = DesktopConfigurationService(config_home=config_home)
            models = ModelSettingsService(config_home / "models.json")
            credentials = SessionCredentialStore()
            window: SettingsWindow | None = None
            try:
                window = SettingsWindow(config, models, credentials)
                window.root.geometry("960x700+25+25")
                window.root.update()
                panel: SettingsPanel = window.panel
                self.assertTrue(panel._first_use.winfo_ismapped())
                self.assertFalse(panel._configured.winfo_ismapped())
                model_ids = {
                    choice.model_id
                    for choice in panel.settings_state.models.embedding_choices
                }
                self.assertEqual(model_ids, {"qwen3-vl-embedding"})
                self.assertTrue(tuple(panel._embedding_box["values"]))

                panel._initial_name.set("首次人物图库")
                panel._initial_images.set(str(images))
                panel._initial_workspace.set(str(workspace))
                panel._initial_results.set(str(results))
                with (
                    mock.patch("zvec_desktop.settings_panel.messagebox.showinfo"),
                    mock.patch(
                        "zvec_desktop.settings_panel.messagebox.showerror"
                    ) as show_error,
                ):
                    panel._create_initial()
                window.root.update()
                show_error.assert_not_called()
                self.assertTrue(panel._configured.winfo_ismapped())
                self.assertEqual(len(panel._library_tree.get_children()), 1)

                secret = "sk-ui-must-clear-immediately"
                panel._credential_entry.insert(0, secret)
                with mock.patch("zvec_desktop.settings_panel.messagebox.showinfo"):
                    panel._save_api_key()
                self.assertTrue(credentials.has_secret())
                self.assertEqual(panel._credential_entry.get(), "")
                status = str(panel._credential_status["text"])
                self.assertIn("已保存", status)
                self.assertNotIn(secret, status)
            except tk.TclError as exc:
                self.skipTest(f"tkinter display is unavailable: {exc}")
            finally:
                if window is not None:
                    window.root.destroy()


if __name__ == "__main__":
    unittest.main()
