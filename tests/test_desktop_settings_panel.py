from __future__ import annotations

import inspect
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
from zvec_desktop.theme import DEFAULT_THEME


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
        self.root = Path(self.temporary.name).resolve()
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

    def test_panel_defaults_to_embedded_mode(self) -> None:
        parameter = inspect.signature(SettingsPanel).parameters["embedded"]

        self.assertIs(parameter.default, True)


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
                self.assertFalse(panel.embedded)
                self.assertIsNotNone(panel._header_title)
                self.assertEqual(panel._notebook.grid_info()["row"], 2)
                self.assertTrue(panel._first_use.winfo_ismapped())
                self.assertFalse(panel._configured.winfo_ismapped())
                self.assertFalse(bool(panel._advanced_section.grid_info()))
                self.assertFalse(bool(panel._add_library_form.grid_info()))
                self.assertTrue(panel._delete_api_key_button.instate(["disabled"]))
                self.assertTrue(panel._config_open_button.instate(["disabled"]))
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
                self.assertTrue(panel._config_open_button.instate(["!disabled"]))
                self.assertEqual(len(panel._library_tree.selection()), 1)
                self.assertEqual(int(panel._library_tree["height"]), 3)
                self.assertIn("共 1 个图库", str(panel._library_summary["text"]))
                self.assertTrue(panel._set_default_button.instate(["disabled"]))
                self.assertTrue(panel._toggle_library_button.instate(["disabled"]))

                panel._toggle_add_library_form()
                window.root.update()
                self.assertTrue(bool(panel._add_library_form.grid_info()))
                self.assertEqual(
                    str(panel._add_library_toggle_button["text"]),
                    "收起图库信息",
                )
                panel._toggle_add_library_form()
                self.assertFalse(bool(panel._add_library_form.grid_info()))

                secret = "sk-ui-must-clear-immediately"
                panel._credential_entry.insert(0, secret)
                with mock.patch("zvec_desktop.settings_panel.messagebox.showinfo"):
                    panel._save_api_key()
                self.assertTrue(credentials.has_secret())
                self.assertEqual(panel._credential_entry.get(), "")
                status = str(panel._credential_status["text"])
                self.assertIn("已保存", status)
                self.assertNotIn(secret, status)
                self.assertEqual(
                    str(panel._credential_status["style"]),
                    "SettingsSuccess.TLabel",
                )
                self.assertEqual(
                    str(panel._credential_card["style"]),
                    "SettingsSuccessCard.TFrame",
                )
                self.assertTrue(panel._delete_api_key_button.instate(["!disabled"]))
            except tk.TclError as exc:
                self.skipTest(f"tkinter display is unavailable: {exc}")
            finally:
                if window is not None:
                    window.root.destroy()

    def test_embedded_panel_removes_duplicate_title_and_uses_modern_styles(
        self,
    ) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"tkinter is unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temporary:
            config_home = Path(temporary).resolve() / "config-home"
            config = DesktopConfigurationService(config_home=config_home)
            models = ModelSettingsService(config_home / "models.json")
            root: tk.Tk | None = None
            try:
                root = tk.Tk()
                panel = SettingsPanel(
                    root,
                    config,
                    models,
                    SessionCredentialStore(),
                )
                panel.pack(fill="both", expand=True)
                root.update_idletasks()

                self.assertTrue(panel.embedded)
                self.assertIsNone(panel._header_title)
                self.assertEqual(panel._notebook.grid_info()["row"], 0)
                self.assertEqual(str(panel["style"]), "SettingsRoot.TFrame")
                self.assertEqual(
                    str(panel._notebook["style"]),
                    "Settings.TNotebook",
                )
                self.assertEqual(
                    str(panel._library_tab["style"]),
                    "SettingsTab.TFrame",
                )
                self.assertEqual(
                    str(panel._library_page["style"]),
                    "SettingsTab.TFrame",
                )
                self.assertEqual(
                    str(panel._library_tree["style"]),
                    "Settings.Treeview",
                )
                self.assertEqual(
                    str(panel._model_json["background"]),
                    DEFAULT_THEME.code_surface,
                )
                self.assertEqual(
                    str(panel._model_json["highlightcolor"]),
                    DEFAULT_THEME.focus,
                )
                self.assertFalse(bool(panel._advanced_section.grid_info()))
                panel._toggle_advanced_editor()
                root.update_idletasks()
                self.assertTrue(bool(panel._advanced_section.grid_info()))
                self.assertEqual(
                    str(panel._advanced_toggle_button["text"]),
                    "收起 JSON 编辑器",
                )
                panel._toggle_advanced_editor()
                self.assertFalse(bool(panel._advanced_section.grid_info()))
            except tk.TclError as exc:
                self.skipTest(f"tkinter display is unavailable: {exc}")
            finally:
                if root is not None:
                    root.destroy()

    def test_compact_embedded_panel_reflows_forms_and_scrolls(self) -> None:
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"tkinter is unavailable: {exc}")

        with tempfile.TemporaryDirectory() as temporary:
            config_home = Path(temporary).resolve() / "config-home"
            config = DesktopConfigurationService(config_home=config_home)
            models = ModelSettingsService(config_home / "models.json")
            root: tk.Tk | None = None
            try:
                root = tk.Tk()
                root.geometry("620x520+30+30")
                panel = SettingsPanel(
                    root,
                    config,
                    models,
                    SessionCredentialStore(),
                )
                panel.pack(fill="both", expand=True)
                for _ in range(4):
                    root.update()

                first_row = panel._responsive_rows[0]
                self.assertTrue(first_row.compact)
                self.assertEqual(int(first_row.label.grid_info()["row"]), 0)
                self.assertEqual(int(first_row.field.grid_info()["row"]), 1)
                scrollbar = panel._tab_scrollbars[panel._library_canvas]
                self.assertTrue(scrollbar.winfo_ismapped())
                self.assertLess(panel._library_canvas.yview()[1], 1.0)

                root.geometry("1100x800+30+30")
                for _ in range(4):
                    root.update()
                self.assertFalse(first_row.compact)
                self.assertEqual(int(first_row.label.grid_info()["row"]), 0)
                self.assertEqual(int(first_row.field.grid_info()["row"]), 0)
            except tk.TclError as exc:
                self.skipTest(f"tkinter display is unavailable: {exc}")
            finally:
                if root is not None:
                    root.destroy()


if __name__ == "__main__":
    unittest.main()
