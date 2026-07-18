"""Independent tkinter settings and first-use panel for the Python desktop.

The panel talks only to injected configuration, model, and credential services.
It never starts a backend, migrates a Collection, or reads an API key back into
the UI.  This keeps first-use writes small, explicit, and independently testable.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .configuration_service import (
    ConfigurationSnapshot,
    DesktopConfigurationError,
    DesktopConfigurationService,
)
from .credentials import (
    CredentialError,
    CredentialStore,
    default_credential_store,
)
from .model_settings import (
    ModelChoice,
    ModelSettingsError,
    ModelSettingsService,
    ModelSettingsSnapshot,
)
from .theme import DEFAULT_THEME

PathOpener = Callable[[Path], None]
RestartNotice = Callable[[str], None]
CredentialNotice = Callable[[bool], None]


class SettingsPanelError(RuntimeError):
    """A settings action cannot be completed without changing unrelated data."""


@dataclass(frozen=True, slots=True)
class SettingsState:
    """One immutable UI snapshot assembled from the three injected services."""

    configuration: ConfigurationSnapshot | None
    models: ModelSettingsSnapshot
    credential_saved: bool
    credential_persistent: bool
    backend_restart_required: bool

    @property
    def configured(self) -> bool:
        return self.configuration is not None


class SettingsController:
    """Synchronous settings coordinator with no dependency on tkinter widgets."""

    def __init__(
        self,
        configuration_service: DesktopConfigurationService,
        model_settings_service: ModelSettingsService,
        credential_store: CredentialStore,
        *,
        path_opener: PathOpener | None = None,
    ) -> None:
        self.configuration_service = configuration_service
        self.model_settings_service = model_settings_service
        self.credential_store = credential_store
        self._path_opener = path_opener or open_with_system
        self._backend_restart_required = False

    @property
    def config_path(self) -> Path:
        return self.configuration_service.path

    @property
    def models_path(self) -> Path:
        return self.model_settings_service.path

    def load(self) -> SettingsState:
        configuration = self.configuration_service.load(optional=True)
        models = self.model_settings_service.load(create=True)
        return SettingsState(
            configuration=configuration,
            models=models,
            credential_saved=self.credential_store.has_secret(),
            credential_persistent=self.credential_store.persistent,
            backend_restart_required=self._backend_restart_required,
        )

    def create_initial_library(
        self,
        image_root: str | Path,
        *,
        name: str | None = None,
        workspace_directory: str | Path | None = None,
        results_directory: str | Path | None = None,
    ) -> SettingsState:
        self.configuration_service.create_initial(
            _required_path(image_root, "图片根目录"),
            name=_optional_text(name),
            workspace_directory=_optional_path(workspace_directory),
            results_directory=_optional_path(results_directory),
        )
        self._backend_restart_required = True
        return self.load()

    def add_library(
        self,
        image_root: str | Path,
        *,
        name: str | None = None,
        workspace_directory: str | Path | None = None,
    ) -> SettingsState:
        self.configuration_service.add_library(
            _required_path(image_root, "图片根目录"),
            name=_optional_text(name),
            workspace_directory=_optional_path(workspace_directory),
        )
        self._backend_restart_required = True
        return self.load()

    def set_library_enabled(self, library_id: str, enabled: bool) -> SettingsState:
        self.configuration_service.set_library_enabled(library_id, enabled)
        self._backend_restart_required = True
        return self.load()

    def set_default_library(self, library_id: str) -> SettingsState:
        self.configuration_service.set_default_library(library_id)
        self._backend_restart_required = True
        return self.load()

    def assign_model_roles(
        self,
        *,
        embedding_model: str,
        auto_tag_primary_model: str,
        auto_tag_escalation_model: str,
    ) -> SettingsState:
        self.model_settings_service.assign_roles(
            embedding_model=embedding_model,
            auto_tag_primary_model=auto_tag_primary_model,
            auto_tag_escalation_model=auto_tag_escalation_model,
        )
        self._backend_restart_required = True
        return self.load()

    def replace_model_json(self, json_text: str) -> SettingsState:
        self.model_settings_service.replace_json(json_text)
        self._backend_restart_required = True
        return self.load()

    def save_api_key(self, secret: str) -> SettingsState:
        self.credential_store.save_secret(secret)
        return self.load()

    def delete_api_key(self) -> SettingsState:
        self.credential_store.delete_secret()
        self._backend_restart_required = True
        return self.load()

    def open_config_file(self) -> None:
        self._open_existing_file(self.config_path, "config.json 尚未创建。")

    def open_models_file(self) -> None:
        self._open_existing_file(self.models_path, "models.json 尚未创建。")

    def _open_existing_file(self, path: Path, missing_message: str) -> None:
        if not path.is_file():
            raise SettingsPanelError(missing_message)
        try:
            self._path_opener(path)
        except OSError as exc:
            raise SettingsPanelError(f"无法使用系统程序打开：{path}") from exc


class SettingsPanel(ttk.Frame):
    """Embeddable first-use and settings surface for the Python desktop."""

    def __init__(
        self,
        master: tk.Misc,
        configuration_service: DesktopConfigurationService,
        model_settings_service: ModelSettingsService,
        credential_store: CredentialStore,
        *,
        path_opener: PathOpener | None = None,
        on_restart_required: RestartNotice | None = None,
        on_credentials_changed: CredentialNotice | None = None,
    ) -> None:
        super().__init__(master, padding=18)
        self.controller = SettingsController(
            configuration_service,
            model_settings_service,
            credential_store,
            path_opener=path_opener,
        )
        self._on_restart_required = on_restart_required
        self._on_credentials_changed = on_credentials_changed
        self._state: SettingsState | None = None
        self._model_maps: dict[str, dict[str, str]] = {}

        self._configure_style()
        self._build_header()
        self._build_notebook()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.refresh(reload_json=True)

    @property
    def settings_state(self) -> SettingsState:
        if self._state is None:
            raise SettingsPanelError("设置面板尚未载入。")
        return self._state

    def refresh(self, *, reload_json: bool = False) -> None:
        try:
            state = self.controller.load()
        except (DesktopConfigurationError, ModelSettingsError, CredentialError) as exc:
            self._show_error("无法载入设置", exc)
            return
        self._apply_state(state, reload_json=reload_json)

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.configure("SettingsTitle.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure(
            "SettingsMuted.TLabel",
            foreground=DEFAULT_THEME.text_muted,
        )
        style.configure(
            "SettingsNotice.TLabel",
            foreground=DEFAULT_THEME.warning,
        )
        style.configure("SettingsDanger.TButton", foreground=DEFAULT_THEME.danger)

    def _build_header(self) -> None:
        ttk.Label(self, text="设置与首次使用", style="SettingsTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            self,
            text="配置只记录路径；不会移动、重建或重新计算现有 Collection。",
            style="SettingsMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 12))

    def _build_notebook(self) -> None:
        self._notebook = ttk.Notebook(self)
        self._notebook.grid(row=2, column=0, sticky="nsew")
        self._library_tab = ttk.Frame(self._notebook, padding=14)
        self._model_tab = ttk.Frame(self._notebook, padding=14)
        self._credential_tab = ttk.Frame(self._notebook, padding=14)
        self._notebook.add(self._library_tab, text="图库与路径")
        self._notebook.add(self._model_tab, text="阿里云模型")
        self._notebook.add(self._credential_tab, text="API Key")
        self._build_library_tab()
        self._build_model_tab()
        self._build_credential_tab()

    def _build_library_tab(self) -> None:
        tab = self._library_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        paths = ttk.LabelFrame(tab, text="配置文件", padding=10)
        paths.grid(row=0, column=0, sticky="ew")
        paths.columnconfigure(1, weight=1)
        self._config_path_var = tk.StringVar(value=str(self.controller.config_path))
        self._models_path_var = tk.StringVar(value=str(self.controller.models_path))
        self._path_row(paths, 0, "图库配置", self._config_path_var, self._open_config)
        self._path_row(paths, 1, "模型配置", self._models_path_var, self._open_models)

        self._first_use = ttk.LabelFrame(tab, text="首次使用", padding=12)
        self._first_use.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self._first_use.columnconfigure(1, weight=1)
        self._initial_name = tk.StringVar()
        self._initial_images = tk.StringVar()
        self._initial_workspace = tk.StringVar()
        self._initial_results = tk.StringVar(
            value=str(self.controller.configuration_service.config_home / "results")
        )
        self._directory_row(
            self._first_use, 0, "图库名称", self._initial_name, directory=False
        )
        self._directory_row(self._first_use, 1, "图片根目录", self._initial_images)
        self._directory_row(self._first_use, 2, "工作区目录", self._initial_workspace)
        self._directory_row(self._first_use, 3, "搜索结果目录", self._initial_results)
        ttk.Label(
            self._first_use,
            text="工作区可选择已有 Collection；程序不会修改或迁移其中的数据。",
            style="SettingsMuted.TLabel",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 8))
        ttk.Button(
            self._first_use,
            text="创建首个图库",
            command=self._create_initial,
        ).grid(row=5, column=2, sticky="e")

        self._configured = ttk.Frame(tab)
        self._configured.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        self._configured.columnconfigure(0, weight=1)
        self._configured.rowconfigure(0, weight=1)
        tree_frame = ttk.Frame(self._configured)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = ("default", "enabled", "name", "images", "workspace")
        self._library_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=8,
        )
        headings = {
            "default": ("默认", 54),
            "enabled": ("状态", 64),
            "name": ("名称", 130),
            "images": ("图片目录", 260),
            "workspace": ("工作区", 260),
        }
        for column, (label, width) in headings.items():
            self._library_tree.heading(column, text=label)
            self._library_tree.column(column, width=width, minwidth=45)
        vertical = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self._library_tree.yview
        )
        horizontal = ttk.Scrollbar(
            tree_frame, orient="horizontal", command=self._library_tree.xview
        )
        self._library_tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        self._library_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        controls = ttk.Frame(self._configured)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(controls, text="设为默认", command=self._set_default).pack(
            side="left"
        )
        ttk.Button(controls, text="启用 / 停用", command=self._toggle_library).pack(
            side="left", padx=(8, 0)
        )

        add = ttk.LabelFrame(self._configured, text="添加图库", padding=10)
        add.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        add.columnconfigure(1, weight=1)
        self._add_name = tk.StringVar()
        self._add_images = tk.StringVar()
        self._add_workspace = tk.StringVar()
        self._directory_row(add, 0, "图库名称", self._add_name, directory=False)
        self._directory_row(add, 1, "图片根目录", self._add_images)
        self._directory_row(add, 2, "工作区（可留空）", self._add_workspace)
        ttk.Button(add, text="添加图库", command=self._add_library).grid(
            row=3, column=2, sticky="e", pady=(8, 0)
        )

    def _build_model_tab(self) -> None:
        tab = self._model_tab
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        roles = ttk.LabelFrame(tab, text="当前模型角色", padding=12)
        roles.grid(row=0, column=0, sticky="ew")
        roles.columnconfigure(1, weight=1)
        self._embedding_var = tk.StringVar()
        self._primary_var = tk.StringVar()
        self._escalation_var = tk.StringVar()
        self._embedding_box = self._model_row(roles, 0, "向量模型", self._embedding_var)
        self._primary_box = self._model_row(roles, 1, "主标注模型", self._primary_var)
        self._escalation_box = self._model_row(
            roles, 2, "升级确认模型", self._escalation_var
        )
        ttk.Button(roles, text="保存角色", command=self._save_roles).grid(
            row=3, column=1, sticky="e", pady=(8, 0)
        )
        self._restart_notice = ttk.Label(
            tab,
            text="模型设置保存后，需要等待任务结束并安全重启后端才会生效。",
            style="SettingsNotice.TLabel",
        )
        self._restart_notice.grid(row=1, column=0, sticky="w", pady=(10, 8))

        advanced = ttk.LabelFrame(tab, text="高级 models.json 编辑", padding=10)
        advanced.grid(row=2, column=0, sticky="nsew")
        advanced.columnconfigure(0, weight=1)
        advanced.rowconfigure(0, weight=1)
        self._model_json = tk.Text(
            advanced,
            wrap="none",
            height=16,
            undo=True,
            font="TkFixedFont",
        )
        json_vertical = ttk.Scrollbar(
            advanced, orient="vertical", command=self._model_json.yview
        )
        json_horizontal = ttk.Scrollbar(
            advanced, orient="horizontal", command=self._model_json.xview
        )
        self._model_json.configure(
            yscrollcommand=json_vertical.set,
            xscrollcommand=json_horizontal.set,
        )
        self._model_json.grid(row=0, column=0, sticky="nsew")
        json_vertical.grid(row=0, column=1, sticky="ns")
        json_horizontal.grid(row=1, column=0, sticky="ew")
        editor_buttons = ttk.Frame(advanced)
        editor_buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(
            editor_buttons,
            text="重新载入",
            command=lambda: self.refresh(reload_json=True),
        ).pack(side="left")
        ttk.Button(
            editor_buttons,
            text="严格校验并保存",
            command=self._save_model_json,
        ).pack(side="left", padx=(8, 0))

    def _build_credential_tab(self) -> None:
        tab = self._credential_tab
        tab.columnconfigure(0, weight=1)
        card = ttk.LabelFrame(tab, text="阿里云 DashScope API Key", padding=14)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="保存状态").grid(row=0, column=0, sticky="w")
        self._credential_status = ttk.Label(card, style="SettingsMuted.TLabel")
        self._credential_status.grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Label(card, text="新的 API Key").grid(
            row=1, column=0, sticky="w", pady=(12, 0)
        )
        self._credential_entry = ttk.Entry(card, show="●")
        self._credential_entry.grid(
            row=1, column=1, sticky="ew", padx=(12, 0), pady=(12, 0)
        )
        ttk.Label(
            card,
            text="出于安全原因，已保存的密钥不会回显；输入框保存后立即清空。",
            style="SettingsMuted.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 12))
        buttons = ttk.Frame(card)
        buttons.grid(row=3, column=1, sticky="e")
        ttk.Button(buttons, text="保存", command=self._save_api_key).pack(side="left")
        ttk.Button(
            buttons,
            text="删除已保存密钥",
            command=self._delete_api_key,
            style="SettingsDanger.TButton",
        ).pack(side="left", padx=(8, 0))

    def _path_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Callable[[], None],
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=variable, state="readonly").grid(
            row=row, column=1, sticky="ew", padx=(10, 8), pady=2
        )
        ttk.Button(parent, text="系统打开", command=command).grid(
            row=row, column=2, pady=2
        )

    def _directory_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        directory: bool = True,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=variable).grid(
            row=row, column=1, sticky="ew", padx=(10, 8), pady=3
        )
        if directory:
            ttk.Button(
                parent,
                text="选择…",
                command=partial(self._choose_directory, variable),
            ).grid(row=row, column=2, pady=3)

    @staticmethod
    def _model_row(
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
    ) -> ttk.Combobox:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        box = ttk.Combobox(parent, textvariable=variable, state="readonly")
        box.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
        return box

    def _apply_state(self, state: SettingsState, *, reload_json: bool) -> None:
        self._state = state
        self._config_path_var.set(str(self.controller.config_path))
        self._models_path_var.set(str(self.controller.models_path))
        if state.configured:
            self._first_use.grid_remove()
            self._configured.grid()
        else:
            self._configured.grid_remove()
            self._first_use.grid()
        self._render_libraries(state.configuration)
        self._render_models(state.models, reload_json=reload_json)
        self._render_credential(state)

    def _render_libraries(self, snapshot: ConfigurationSnapshot | None) -> None:
        for item in self._library_tree.get_children():
            self._library_tree.delete(item)
        if snapshot is None:
            return
        default_id = snapshot.configuration.default_library_id
        for library in snapshot.libraries:
            self._library_tree.insert(
                "",
                "end",
                iid=library.library_id,
                values=(
                    "是" if library.library_id == default_id else "",
                    "已启用" if library.enabled else "已停用",
                    library.name,
                    str(library.image_root),
                    str(library.workspace_directory),
                ),
            )

    def _render_models(
        self, snapshot: ModelSettingsSnapshot, *, reload_json: bool
    ) -> None:
        role_data = (
            (
                "embedding",
                self._embedding_box,
                self._embedding_var,
                snapshot.embedding_choices,
                snapshot.embedding_model,
            ),
            (
                "primary",
                self._primary_box,
                self._primary_var,
                snapshot.auto_tag_primary_choices,
                snapshot.auto_tag_primary_model,
            ),
            (
                "escalation",
                self._escalation_box,
                self._escalation_var,
                snapshot.auto_tag_escalation_choices,
                snapshot.auto_tag_escalation_model,
            ),
        )
        self._model_maps.clear()
        for key, box, variable, choices, selected_id in role_data:
            mapping = {_choice_label(choice): choice.model_id for choice in choices}
            reverse = {model_id: label for label, model_id in mapping.items()}
            self._model_maps[key] = mapping
            box.configure(values=tuple(mapping))
            variable.set(reverse.get(selected_id, ""))
        if reload_json:
            self._model_json.delete("1.0", "end")
            self._model_json.insert("1.0", snapshot.json_text)
        if snapshot.provider != "aliyun_dashscope":
            raise SettingsPanelError("当前仅支持阿里云 DashScope 模型配置。")

    def _render_credential(self, state: SettingsState) -> None:
        if state.credential_saved:
            storage = "Windows 安全凭据" if state.credential_persistent else "本次会话"
            text = f"已保存（{storage}）"
        else:
            text = "尚未保存"
        self._credential_status.configure(text=text)

    def _choose_directory(self, variable: tk.StringVar) -> None:
        selected = filedialog.askdirectory(
            parent=self.winfo_toplevel(),
            initialdir=variable.get() or str(Path.home()),
            mustexist=False,
        )
        if selected:
            variable.set(selected)

    def _create_initial(self) -> None:
        self._perform(
            lambda: self.controller.create_initial_library(
                self._initial_images.get(),
                name=self._initial_name.get(),
                workspace_directory=self._initial_workspace.get(),
                results_directory=self._initial_results.get(),
            ),
            success="首个图库配置已创建。",
            reload_json=True,
            restart_reason="图库配置已创建",
        )

    def _add_library(self) -> None:
        self._perform(
            lambda: self.controller.add_library(
                self._add_images.get(),
                name=self._add_name.get(),
                workspace_directory=self._add_workspace.get(),
            ),
            success="图库已添加；现有工作区内容未被修改。",
            restart_reason="图库已添加",
        )

    def _selected_library_id(self) -> str:
        selected = self._library_tree.selection()
        if len(selected) != 1:
            raise SettingsPanelError("请先选择一个图库。")
        return selected[0]

    def _set_default(self) -> None:
        try:
            library_id = self._selected_library_id()
        except SettingsPanelError as exc:
            self._show_error("无法设置默认图库", exc)
            return
        self._perform(
            lambda: self.controller.set_default_library(library_id),
            success="默认图库已更新。",
            restart_reason="默认图库已更新",
        )

    def _toggle_library(self) -> None:
        try:
            library_id = self._selected_library_id()
            snapshot = self.settings_state.configuration
            if snapshot is None:
                raise SettingsPanelError("尚未配置图库。")
            library = next(
                item for item in snapshot.libraries if item.library_id == library_id
            )
        except (SettingsPanelError, StopIteration) as exc:
            self._show_error("无法更新图库状态", exc)
            return
        self._perform(
            lambda: self.controller.set_library_enabled(
                library_id,
                not library.enabled,
            ),
            success="图库状态已更新。",
            restart_reason="图库状态已更新",
        )

    def _save_roles(self) -> None:
        try:
            embedding = self._model_maps["embedding"][self._embedding_var.get()]
            primary = self._model_maps["primary"][self._primary_var.get()]
            escalation = self._model_maps["escalation"][self._escalation_var.get()]
        except KeyError:
            self._show_error(
                "无法保存模型角色", SettingsPanelError("请选择全部模型角色。")
            )
            return
        self._perform(
            lambda: self.controller.assign_model_roles(
                embedding_model=embedding,
                auto_tag_primary_model=primary,
                auto_tag_escalation_model=escalation,
            ),
            success="模型角色已保存；请安全重启后端后使用。",
            reload_json=True,
            restart_reason="模型角色已更新",
        )

    def _save_model_json(self) -> None:
        json_text = self._model_json.get("1.0", "end-1c")
        self._perform(
            lambda: self.controller.replace_model_json(json_text),
            success="models.json 已严格校验并保存；请安全重启后端。",
            reload_json=True,
            restart_reason="models.json 已更新",
        )

    def _save_api_key(self) -> None:
        secret = self._credential_entry.get()
        try:
            state = self.controller.save_api_key(secret)
        except (CredentialError, OSError, ValueError) as exc:
            self._credential_entry.delete(0, "end")
            self._show_error("无法保存 API Key", exc)
            return
        finally:
            secret = ""
        self._credential_entry.delete(0, "end")
        self._apply_state(state, reload_json=False)
        if self._on_credentials_changed is not None:
            self._on_credentials_changed(True)
        messagebox.showinfo(
            "API Key", "API Key 已安全保存。", parent=self.winfo_toplevel()
        )

    def _delete_api_key(self) -> None:
        if not messagebox.askyesno(
            "删除 API Key",
            "确定删除已保存的 API Key？",
            parent=self.winfo_toplevel(),
        ):
            return
        try:
            state = self.controller.delete_api_key()
        except (CredentialError, OSError) as exc:
            self._show_error("无法删除 API Key", exc)
            return
        self._credential_entry.delete(0, "end")
        self._apply_state(state, reload_json=False)
        if self._on_credentials_changed is not None:
            self._on_credentials_changed(False)
        if self._on_restart_required is not None:
            self._on_restart_required("API Key 已删除")

    def _open_config(self) -> None:
        self._open_path(self.controller.open_config_file)

    def _open_models(self) -> None:
        self._open_path(self.controller.open_models_file)

    def _open_path(self, action: Callable[[], None]) -> None:
        try:
            action()
        except SettingsPanelError as exc:
            self._show_error("无法打开配置文件", exc)

    def _perform(
        self,
        action: Callable[[], SettingsState],
        *,
        success: str,
        reload_json: bool = False,
        restart_reason: str | None = None,
    ) -> None:
        try:
            state = action()
        except (
            CredentialError,
            DesktopConfigurationError,
            ModelSettingsError,
            OSError,
            SettingsPanelError,
            ValueError,
        ) as exc:
            self._show_error("无法保存设置", exc)
            return
        self._apply_state(state, reload_json=reload_json)
        if restart_reason is not None and self._on_restart_required is not None:
            self._on_restart_required(restart_reason)
        messagebox.showinfo("设置", success, parent=self.winfo_toplevel())

    def _show_error(self, title: str, error: BaseException) -> None:
        messagebox.showerror(title, str(error), parent=self.winfo_toplevel())


class SettingsWindow:
    """Standalone host used before the panel is embedded in the main shell."""

    def __init__(
        self,
        configuration_service: DesktopConfigurationService | None = None,
        model_settings_service: ModelSettingsService | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        configuration = (
            configuration_service
            if configuration_service is not None
            else DesktopConfigurationService()
        )
        models = (
            model_settings_service
            if model_settings_service is not None
            else ModelSettingsService(configuration.config_home / "models.json")
        )
        credentials = (
            credential_store
            if credential_store is not None
            else default_credential_store()
        )
        self.root = tk.Tk()
        self.root.title("Zvec 设置与首次使用")
        self.root.geometry("1020x760")
        self.root.minsize(820, 620)
        self.root.configure(background=DEFAULT_THEME.window)
        self.panel = SettingsPanel(
            self.root,
            configuration,
            models,
            credentials,
        )
        self.panel.pack(fill="both", expand=True)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    """Run the settings panel as a standalone Python window."""

    try:
        SettingsWindow().run()
    except (OSError, RuntimeError, tk.TclError) as exc:
        print(f"Unable to open Zvec settings: {exc}", file=sys.stderr)
        return 1
    return 0


def open_with_system(path: Path) -> None:
    """Open a validated existing file using the platform default application."""

    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise OSError(f"Path is not a file: {resolved}")
    if os.name == "nt":
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise OSError("Windows system-open API is unavailable.")
        startfile(str(resolved))
        return
    command = (
        ["open", str(resolved)]
        if sys.platform == "darwin"
        else [
            "xdg-open",
            str(resolved),
        ]
    )
    subprocess.Popen(command, close_fds=True)  # noqa: S603 - no shell is used.


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _optional_path(value: str | Path | None) -> str | Path | None:
    if value is None or isinstance(value, Path):
        return value
    normalized = value.strip()
    return normalized or None


def _required_path(value: str | Path, label: str) -> str | Path:
    if isinstance(value, str) and not value.strip():
        raise SettingsPanelError(f"请选择{label}。")
    return value


def _choice_label(choice: ModelChoice) -> str:
    return f"{choice.display_name}  ·  {choice.model_id}"


__all__ = [
    "SettingsController",
    "SettingsPanel",
    "SettingsPanelError",
    "SettingsState",
    "SettingsWindow",
    "main",
    "open_with_system",
]


if __name__ == "__main__":
    raise SystemExit(main())
