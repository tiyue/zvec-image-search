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
from contextlib import suppress
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


@dataclass(slots=True)
class _ResponsiveFormRow:
    """Widgets that switch between inline and stacked form layouts."""

    container: ttk.Frame
    label: ttk.Label
    field: tk.Widget
    action: ttk.Button | None
    compact: bool | None = None


class SettingsPanel(ttk.Frame):
    """Embeddable first-use and settings surface for the Python desktop."""

    _FORM_COMPACT_WIDTH = 640

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
        embedded: bool = True,
    ) -> None:
        spacing = DEFAULT_THEME.spacing
        super().__init__(
            master,
            style="SettingsRoot.TFrame",
            padding=0 if embedded else spacing.lg,
        )
        self.embedded = embedded
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
        self._header_title: ttk.Label | None = None
        self._notebook_row = 0 if embedded else 2
        self._responsive_rows: list[_ResponsiveFormRow] = []
        self._tab_scrollbars: dict[tk.Canvas, ttk.Scrollbar] = {}
        self._advanced_visible = False
        self._add_library_visible = False

        self._configure_style()
        if not embedded:
            self._build_header()
        self._build_notebook()
        self.columnconfigure(0, weight=1)
        self.rowconfigure(self._notebook_row, weight=1)
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
        with suppress(tk.TclError):
            style.theme_use("clam")
        theme = DEFAULT_THEME
        font = theme.typography.family
        spacing = theme.spacing
        style.configure("SettingsRoot.TFrame", background=theme.window)
        style.configure("SettingsSurface.TFrame", background=theme.surface)
        style.configure("SettingsSoft.TFrame", background=theme.surface_subtle)
        style.configure("SettingsSuccessCard.TFrame", background=theme.success_soft)
        style.configure("SettingsDangerZone.TFrame", background=theme.danger_soft)
        style.configure(
            "SettingsTitle.TLabel",
            background=theme.window,
            foreground=theme.text,
            font=(font, theme.typography.page_title, "bold"),
        )
        style.configure(
            "SettingsTitleMuted.TLabel",
            background=theme.window,
            foreground=theme.text_muted,
            font=(font, theme.typography.body),
        )
        style.configure(
            "SettingsLabel.TLabel",
            background=theme.surface,
            foreground=theme.text,
            font=(font, theme.typography.body),
        )
        style.configure(
            "SettingsPageTitle.TLabel",
            background=theme.surface,
            foreground=theme.text,
            font=(font, theme.typography.preview_title, "bold"),
        )
        style.configure(
            "SettingsPageCopy.TLabel",
            background=theme.surface,
            foreground=theme.text_muted,
            font=(font, theme.typography.body),
        )
        style.configure(
            "SettingsStep.TLabel",
            background=theme.primary_soft,
            foreground=theme.primary_pressed,
            font=(font, theme.typography.supporting, "bold"),
            padding=(spacing.sm, spacing.xs),
        )
        style.configure(
            "SettingsSectionTitle.TLabel",
            background=theme.surface,
            foreground=theme.text,
            font=(font, theme.typography.section, "bold"),
        )
        style.configure(
            "SettingsSectionCopy.TLabel",
            background=theme.surface,
            foreground=theme.text_muted,
            font=(font, theme.typography.supporting),
        )
        style.configure(
            "SettingsSoftTitle.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text,
            font=(font, theme.typography.body, "bold"),
        )
        style.configure(
            "SettingsSoftLabel.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text,
            font=(font, theme.typography.body),
        )
        style.configure(
            "SettingsSoftMuted.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text_muted,
            font=(font, theme.typography.supporting),
        )
        style.configure(
            "SettingsDangerTitle.TLabel",
            background=theme.danger_soft,
            foreground=theme.danger,
            font=(font, theme.typography.body, "bold"),
        )
        style.configure(
            "SettingsDangerCopy.TLabel",
            background=theme.danger_soft,
            foreground=theme.text_muted,
            font=(font, theme.typography.supporting),
        )
        style.configure(
            "SettingsMuted.TLabel",
            background=theme.surface,
            foreground=theme.text_muted,
            font=(font, theme.typography.supporting),
        )
        style.configure(
            "SettingsNotice.TLabel",
            background=theme.warning_soft,
            foreground=theme.warning,
            font=(font, theme.typography.body),
            padding=(spacing.md, spacing.sm),
        )
        style.configure(
            "SettingsSuccess.TLabel",
            background=theme.success_soft,
            foreground=theme.success,
            font=(font, theme.typography.body, "bold"),
        )
        style.configure(
            "SettingsSuccessTitle.TLabel",
            background=theme.success_soft,
            foreground=theme.success,
            font=(font, theme.typography.body, "bold"),
        )
        style.configure(
            "SettingsSuccessMuted.TLabel",
            background=theme.success_soft,
            foreground=theme.text_muted,
            font=(font, theme.typography.supporting),
        )
        style.configure(
            "SettingsCredentialMuted.TLabel",
            background=theme.surface_subtle,
            foreground=theme.text_muted,
            font=(font, theme.typography.body, "bold"),
        )
        style.configure(
            "Settings.TNotebook",
            background=theme.window,
            bordercolor=theme.border,
            lightcolor=theme.border,
            darkcolor=theme.border,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "Settings.TNotebook.Tab",
            background=theme.window,
            foreground=theme.text_muted,
            bordercolor=theme.window,
            lightcolor=theme.window,
            darkcolor=theme.window,
            padding=(spacing.lg, spacing.sm + 2),
            font=(font, theme.typography.control, "bold"),
        )
        style.map(
            "Settings.TNotebook.Tab",
            background=[
                ("selected", theme.surface),
                ("active", theme.primary_soft),
            ],
            foreground=[
                ("selected", theme.primary_pressed),
                ("active", theme.primary),
            ],
            expand=[("selected", (0, 0, 0, spacing.xs))],
        )
        style.configure("SettingsTab.TFrame", background=theme.surface)
        style.configure(
            "SettingsCard.TLabelframe",
            background=theme.surface,
            bordercolor=theme.border,
            lightcolor=theme.border,
            darkcolor=theme.border,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "SettingsCard.TLabelframe.Label",
            background=theme.surface,
            foreground=theme.text,
            font=(font, theme.typography.section, "bold"),
            padding=(0, 0, spacing.sm, spacing.xs),
        )
        style.configure(
            "Settings.TEntry",
            fieldbackground=theme.surface,
            foreground=theme.text,
            bordercolor=theme.border_strong,
            lightcolor=theme.border_strong,
            darkcolor=theme.border_strong,
            insertcolor=theme.primary,
            padding=(spacing.sm, spacing.sm),
            borderwidth=1,
        )
        style.map(
            "Settings.TEntry",
            fieldbackground=[("readonly", theme.surface_subtle)],
            bordercolor=[("focus", theme.focus)],
            lightcolor=[("focus", theme.focus)],
            darkcolor=[("focus", theme.focus)],
        )
        style.configure(
            "Settings.TCombobox",
            fieldbackground=theme.surface,
            background=theme.surface,
            foreground=theme.text,
            arrowcolor=theme.text_muted,
            bordercolor=theme.border_strong,
            lightcolor=theme.border_strong,
            darkcolor=theme.border_strong,
            padding=(spacing.sm, spacing.sm),
        )
        style.map(
            "Settings.TCombobox",
            fieldbackground=[("readonly", theme.surface)],
            bordercolor=[("focus", theme.focus)],
            lightcolor=[("focus", theme.focus)],
            darkcolor=[("focus", theme.focus)],
        )
        style.configure(
            "SettingsPrimary.TButton",
            background=theme.primary,
            foreground="#FFFFFF",
            bordercolor=theme.primary,
            lightcolor=theme.primary,
            darkcolor=theme.primary,
            focuscolor=theme.primary,
            borderwidth=1,
            padding=(spacing.md, spacing.sm),
            font=(font, theme.typography.control, "bold"),
        )
        style.map(
            "SettingsPrimary.TButton",
            background=[
                ("pressed", theme.primary_pressed),
                ("active", theme.primary_hover),
                ("disabled", theme.surface_strong),
            ],
            bordercolor=[
                ("pressed", theme.primary_pressed),
                ("active", theme.primary_hover),
                ("disabled", theme.surface_strong),
            ],
            foreground=[("disabled", theme.text_faint)],
        )
        style.configure(
            "SettingsQuiet.TButton",
            background=theme.surface,
            foreground=theme.text,
            bordercolor=theme.border_strong,
            lightcolor=theme.border_strong,
            darkcolor=theme.border_strong,
            focuscolor=theme.focus,
            borderwidth=1,
            padding=(spacing.md, spacing.sm),
            font=(font, theme.typography.control),
        )
        style.map(
            "SettingsQuiet.TButton",
            background=[
                ("active", theme.surface_muted),
                ("disabled", theme.surface_subtle),
            ],
            foreground=[("disabled", theme.text_faint)],
        )
        style.configure(
            "SettingsDanger.TButton",
            background=theme.surface,
            foreground=theme.danger,
            bordercolor=theme.danger_soft,
            lightcolor=theme.danger_soft,
            darkcolor=theme.danger_soft,
            focuscolor=theme.danger,
            borderwidth=1,
            padding=(spacing.md, spacing.sm),
            font=(font, theme.typography.control),
        )
        style.map(
            "SettingsDanger.TButton",
            background=[("active", theme.danger_soft)],
        )
        style.configure(
            "Settings.Treeview",
            background=theme.surface,
            fieldbackground=theme.surface,
            foreground=theme.text,
            bordercolor=theme.border,
            borderwidth=0,
            relief="flat",
            rowheight=30,
        )
        style.map(
            "Settings.Treeview",
            background=[("selected", theme.selection)],
            foreground=[("selected", theme.text)],
        )
        style.configure(
            "Settings.Treeview.Heading",
            background=theme.surface_muted,
            foreground=theme.text_muted,
            bordercolor=theme.border,
            borderwidth=0,
            padding=(spacing.sm, spacing.sm),
            font=(font, theme.typography.supporting, "bold"),
        )
        style.map(
            "Settings.Treeview.Heading",
            background=[("active", theme.surface_strong)],
        )
        for orientation in ("Vertical", "Horizontal"):
            style.configure(
                f"Settings.{orientation}.TScrollbar",
                background=theme.border_strong,
                troughcolor=theme.surface_subtle,
                bordercolor=theme.surface_subtle,
                arrowcolor=theme.text_muted,
            )

    def _build_header(self) -> None:
        self._header_title = ttk.Label(
            self,
            text="设置与首次使用",
            style="SettingsTitle.TLabel",
        )
        self._header_title.grid(row=0, column=0, sticky="w")
        ttk.Label(
            self,
            text="配置只记录路径；不会移动、重建或重新计算现有 Collection。",
            style="SettingsTitleMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 12))

    def _build_notebook(self) -> None:
        spacing = DEFAULT_THEME.spacing
        self._notebook = ttk.Notebook(self, style="Settings.TNotebook")
        self._notebook.grid(row=self._notebook_row, column=0, sticky="nsew")
        tab_padding = (spacing.lg, spacing.md, spacing.lg, spacing.lg)
        self._library_tab = ttk.Frame(
            self._notebook,
            style="SettingsTab.TFrame",
        )
        self._model_tab = ttk.Frame(
            self._notebook,
            style="SettingsTab.TFrame",
        )
        self._credential_tab = ttk.Frame(
            self._notebook,
            style="SettingsTab.TFrame",
        )
        self._notebook.add(self._library_tab, text="图库与路径")
        self._notebook.add(self._model_tab, text="阿里云模型")
        self._notebook.add(self._credential_tab, text="API Key")
        self._library_page, self._library_canvas = self._scrollable_tab(
            self._library_tab,
            padding=tab_padding,
        )
        self._model_page, self._model_canvas = self._scrollable_tab(
            self._model_tab,
            padding=tab_padding,
        )
        self._credential_page, self._credential_canvas = self._scrollable_tab(
            self._credential_tab,
            padding=tab_padding,
        )
        self._build_library_tab()
        self._build_model_tab()
        self._build_credential_tab()
        self._bind_scroll_wheel(self._library_page, self._library_canvas)
        self._bind_scroll_wheel(self._model_page, self._model_canvas)
        self._bind_scroll_wheel(self._credential_page, self._credential_canvas)

    def _scrollable_tab(
        self,
        host: ttk.Frame,
        *,
        padding: tuple[int, int, int, int],
    ) -> tuple[ttk.Frame, tk.Canvas]:
        """Create a borderless page whose scrollbar appears only when needed."""

        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        canvas = tk.Canvas(
            host,
            background=DEFAULT_THEME.surface,
            highlightthickness=0,
            borderwidth=0,
        )
        scrollbar = ttk.Scrollbar(
            host,
            orient="vertical",
            command=canvas.yview,
            style="Settings.Vertical.TScrollbar",
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        scrollbar.grid_remove()
        self._tab_scrollbars[canvas] = scrollbar
        page = ttk.Frame(
            canvas,
            style="SettingsTab.TFrame",
            padding=padding,
        )
        window = canvas.create_window((0, 0), window=page, anchor="nw")

        def sync_scroll_region(_event: tk.Event[tk.Misc] | None = None) -> None:
            bounds = canvas.bbox("all")
            canvas.configure(scrollregion=bounds)
            needs_scroll = page.winfo_reqheight() > canvas.winfo_height() + 2
            if needs_scroll:
                scrollbar.grid()
            else:
                scrollbar.grid_remove()
                canvas.yview_moveto(0.0)

        def fit_width(event: tk.Event[tk.Misc]) -> None:
            canvas.itemconfigure(window, width=max(1, event.width))
            canvas.after_idle(sync_scroll_region)

        page.bind("<Configure>", sync_scroll_region, add=True)
        canvas.bind("<Configure>", fit_width, add=True)
        return page, canvas

    def _bind_scroll_wheel(self, page: ttk.Frame, canvas: tk.Canvas) -> None:
        """Route wheel events from every current page child to its canvas."""

        def scroll(event: tk.Event[tk.Misc]) -> str:
            delta = -1 if event.delta > 0 else 1
            canvas.yview_scroll(delta * 3, "units")
            return "break"

        def bind_tree(widget: tk.Misc) -> None:
            # Editors and the library table keep their own scrolling.  Every
            # other surface routes the wheel to the page so short windows do
            # not trap users above the primary action.
            if not isinstance(widget, (tk.Text, ttk.Treeview)):
                widget.bind("<MouseWheel>", scroll, add=True)
            for child in widget.winfo_children():
                bind_tree(child)

        bind_tree(page)

    def _build_tab_intro(
        self,
        parent: ttk.Frame,
        *,
        row: int,
        step: str,
        title: str,
        description: str,
    ) -> ttk.Frame:
        """Add a consistent step marker and concise page-level explanation."""

        intro = ttk.Frame(parent, style="SettingsSurface.TFrame")
        intro.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        intro.columnconfigure(1, weight=1)
        ttk.Label(intro, text=step, style="SettingsStep.TLabel").grid(
            row=0,
            column=0,
            rowspan=2,
            sticky="n",
            padx=(0, 12),
        )
        ttk.Label(intro, text=title, style="SettingsPageTitle.TLabel").grid(
            row=0,
            column=1,
            sticky="w",
        )
        copy = ttk.Label(
            intro,
            text=description,
            style="SettingsPageCopy.TLabel",
            wraplength=240,
            justify="left",
        )
        copy.grid(row=1, column=1, sticky="ew", pady=(3, 0))
        self._bind_responsive_wrap(copy, intro, reserved=60)
        return intro

    def _section(
        self,
        parent: ttk.Frame,
        *,
        row: int,
        title: str,
        description: str,
        sticky: str = "ew",
        pady: tuple[int, int] = (0, 14),
    ) -> tuple[ttk.Frame, ttk.Frame]:
        """Build a borderless section with a soft content surface."""

        section = ttk.Frame(parent, style="SettingsSurface.TFrame")
        section.grid(row=row, column=0, sticky=sticky, pady=pady)
        section.columnconfigure(0, weight=1)
        if "n" in sticky or "s" in sticky:
            section.rowconfigure(2, weight=1)
        ttk.Label(
            section,
            text=title,
            style="SettingsSectionTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        copy = ttk.Label(
            section,
            text=description,
            style="SettingsSectionCopy.TLabel",
            wraplength=240,
            justify="left",
        )
        copy.grid(row=1, column=0, sticky="ew", pady=(2, 8))
        self._bind_responsive_wrap(copy, section)
        body = ttk.Frame(
            section,
            style="SettingsSoft.TFrame",
            padding=(14, 12),
        )
        body.grid(row=2, column=0, sticky=sticky)
        body.columnconfigure(0, weight=1)
        return section, body

    @staticmethod
    def _bind_responsive_wrap(
        label: ttk.Label,
        parent: tk.Misc,
        *,
        reserved: int = 0,
    ) -> None:
        """Keep explanatory copy readable instead of clipping on narrow pages."""

        def resize(event: tk.Event[tk.Misc]) -> None:
            label.configure(wraplength=max(220, event.width - reserved))

        parent.bind("<Configure>", resize, add=True)

    def _build_library_tab(self) -> None:
        tab = self._library_page
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        self._build_tab_intro(
            tab,
            row=0,
            step="1",
            title="图库与路径",
            description="先确认配置文件位置，再创建或维护图库。程序只记录路径，不会移动图片或重新生成已有向量。",
        )
        self._paths_section, paths = self._section(
            tab,
            row=1,
            title="配置文件",
            description="需要手工备份或排查问题时，可直接打开对应的 JSON 文件。",
            pady=(0, 14),
        )
        self._config_path_var = tk.StringVar(value=str(self.controller.config_path))
        self._models_path_var = tk.StringVar(value=str(self.controller.models_path))
        self._config_open_button = self._path_row(
            paths,
            0,
            "图库配置",
            self._config_path_var,
            self._open_config,
        )
        self._models_open_button = self._path_row(
            paths,
            1,
            "模型配置",
            self._models_path_var,
            self._open_models,
        )

        self._first_use, first_use = self._section(
            tab,
            row=2,
            title="创建首个图库",
            description="当前还没有图库。完成下面四项后即可开始索引和搜索。",
            sticky="nsew",
            pady=(0, 0),
        )
        first_use.columnconfigure(0, weight=1)
        first_use_note = ttk.Label(
            first_use,
            text="空图库不会产生模型费用；建议先选择 10～50 张图片的小目录验证流程。",
            style="SettingsSoftMuted.TLabel",
            wraplength=240,
            justify="left",
        )
        first_use_note.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self._bind_responsive_wrap(first_use_note, first_use)
        self._initial_name = tk.StringVar()
        self._initial_images = tk.StringVar()
        self._initial_workspace = tk.StringVar()
        self._initial_results = tk.StringVar(
            value=str(self.controller.configuration_service.config_home / "results")
        )
        self._directory_row(
            first_use, 1, "图库名称", self._initial_name, directory=False
        )
        self._directory_row(first_use, 2, "图片根目录", self._initial_images)
        self._directory_row(first_use, 3, "工作区目录", self._initial_workspace)
        self._directory_row(first_use, 4, "搜索结果目录", self._initial_results)
        workspace_note = ttk.Label(
            first_use,
            text="工作区可选择已有 Collection；程序不会修改或迁移其中的数据。",
            style="SettingsSoftMuted.TLabel",
            wraplength=240,
            justify="left",
        )
        workspace_note.grid(row=5, column=0, sticky="ew", pady=(6, 10))
        self._bind_responsive_wrap(workspace_note, first_use)
        self._create_library_button = ttk.Button(
            first_use,
            text="创建首个图库",
            command=self._create_initial,
            style="SettingsPrimary.TButton",
        )
        self._create_library_button.grid(row=6, column=0, sticky="e")

        self._configured, configured = self._section(
            tab,
            row=2,
            title="已配置图库",
            description="选择一项后可设为默认或调整启用状态；路径内容只读展示。",
            sticky="nsew",
            pady=(0, 0),
        )
        configured.rowconfigure(1, weight=1)
        self._library_summary = ttk.Label(
            configured,
            style="SettingsSoftMuted.TLabel",
        )
        self._library_summary.grid(row=0, column=0, sticky="w", pady=(0, 8))
        tree_frame = ttk.Frame(configured, style="SettingsSoft.TFrame")
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = ("default", "enabled", "name", "images", "workspace")
        self._library_tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=5,
            style="Settings.Treeview",
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
            tree_frame,
            orient="vertical",
            command=self._library_tree.yview,
            style="Settings.Vertical.TScrollbar",
        )
        horizontal = ttk.Scrollbar(
            tree_frame,
            orient="horizontal",
            command=self._library_tree.xview,
            style="Settings.Horizontal.TScrollbar",
        )
        self._library_tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )
        self._library_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self._library_tree.bind(
            "<<TreeviewSelect>>",
            self._on_library_selection_changed,
            add=True,
        )
        controls = ttk.Frame(configured, style="SettingsSoft.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._set_default_button = ttk.Button(
            controls,
            text="设为默认",
            command=self._set_default,
            style="SettingsQuiet.TButton",
        )
        self._set_default_button.pack(side="left")
        self._toggle_library_button = ttk.Button(
            controls,
            text="调整启用状态",
            command=self._toggle_library,
            style="SettingsQuiet.TButton",
        )
        self._toggle_library_button.pack(side="left", padx=(8, 0))

        ttk.Separator(configured, orient="horizontal").grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(14, 10),
        )
        self._add_library_teaser = ttk.Frame(
            configured,
            style="SettingsSoft.TFrame",
        )
        self._add_library_teaser.grid(row=4, column=0, sticky="ew")
        self._add_library_teaser.columnconfigure(0, weight=1)
        ttk.Label(
            self._add_library_teaser,
            text="添加另一个图库",
            style="SettingsSoftTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            self._add_library_teaser,
            text="工作区可留空，程序将使用默认位置。",
            style="SettingsSoftMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._add_library_toggle_button = ttk.Button(
            self._add_library_teaser,
            text="填写图库信息",
            command=self._toggle_add_library_form,
            style="SettingsQuiet.TButton",
        )
        self._add_library_toggle_button.grid(
            row=0,
            column=1,
            rowspan=2,
            sticky="e",
            padx=(12, 0),
        )
        self._add_library_form = ttk.Frame(
            configured,
            style="SettingsSoft.TFrame",
        )
        self._add_library_form.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self._add_library_form.columnconfigure(0, weight=1)
        self._add_name = tk.StringVar()
        self._add_images = tk.StringVar()
        self._add_workspace = tk.StringVar()
        self._directory_row(
            self._add_library_form,
            0,
            "图库名称",
            self._add_name,
            directory=False,
        )
        self._directory_row(
            self._add_library_form,
            1,
            "图片根目录",
            self._add_images,
        )
        self._directory_row(
            self._add_library_form,
            2,
            "工作区（可留空）",
            self._add_workspace,
        )
        self._add_library_button = ttk.Button(
            self._add_library_form,
            text="添加图库",
            command=self._add_library,
            style="SettingsPrimary.TButton",
        )
        self._add_library_button.grid(row=3, column=0, sticky="e", pady=(8, 0))
        self._add_library_form.grid_remove()

    def _build_model_tab(self) -> None:
        tab = self._model_page
        tab.columnconfigure(0, weight=1)
        self._build_tab_intro(
            tab,
            row=0,
            step="2",
            title="阿里云模型",
            description=(
                "为向量检索和自动标注分配模型。通常只需确认三个角色，无需编辑 JSON。"
            ),
        )
        self._roles_section, roles = self._section(
            tab,
            row=1,
            title="模型角色",
            description="向量模型负责检索；主标注模型处理常规图片；升级确认模型处理不确定身份。",
            pady=(0, 10),
        )
        self._embedding_var = tk.StringVar()
        self._primary_var = tk.StringVar()
        self._escalation_var = tk.StringVar()
        self._embedding_box = self._model_row(roles, 0, "向量模型", self._embedding_var)
        self._primary_box = self._model_row(roles, 1, "主标注模型", self._primary_var)
        self._escalation_box = self._model_row(
            roles, 2, "升级确认模型", self._escalation_var
        )
        self._save_roles_button = ttk.Button(
            roles,
            text="保存角色",
            command=self._save_roles,
            style="SettingsPrimary.TButton",
        )
        self._save_roles_button.grid(row=3, column=0, sticky="e", pady=(8, 0))
        self._restart_notice = ttk.Label(
            tab,
            text="模型设置保存后，需要等待任务结束并安全重启后端才会生效。",
            style="SettingsNotice.TLabel",
            wraplength=240,
            justify="left",
        )
        self._restart_notice.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self._bind_responsive_wrap(self._restart_notice, tab)

        self._advanced_teaser = ttk.Frame(
            tab,
            style="SettingsSoft.TFrame",
            padding=(14, 10),
        )
        self._advanced_teaser.grid(row=3, column=0, sticky="ew")
        self._advanced_teaser.columnconfigure(0, weight=1)
        ttk.Label(
            self._advanced_teaser,
            text="高级设置",
            style="SettingsSoftTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._advanced_teaser_copy = ttk.Label(
            self._advanced_teaser,
            text="仅在添加模型定义、协议或价格信息时编辑 models.json。",
            style="SettingsSoftMuted.TLabel",
            wraplength=240,
            justify="left",
        )
        self._advanced_teaser_copy.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self._bind_responsive_wrap(
            self._advanced_teaser_copy,
            self._advanced_teaser,
            reserved=160,
        )
        self._advanced_toggle_button = ttk.Button(
            self._advanced_teaser,
            text="展开 JSON 编辑器",
            command=self._toggle_advanced_editor,
            style="SettingsQuiet.TButton",
        )
        self._advanced_toggle_button.grid(
            row=0,
            column=1,
            rowspan=2,
            sticky="e",
            padx=(12, 0),
        )

        self._advanced_section, advanced = self._section(
            tab,
            row=4,
            title="高级 models.json 编辑",
            description="保存前会严格校验提供商、模型角色和字段格式；无效内容不会写入磁盘。",
            sticky="nsew",
            pady=(12, 0),
        )
        advanced.rowconfigure(0, weight=1)
        self._model_json = tk.Text(
            advanced,
            wrap="none",
            height=12,
            undo=True,
            font="TkFixedFont",
            background=DEFAULT_THEME.code_surface,
            foreground=DEFAULT_THEME.code_text,
            insertbackground=DEFAULT_THEME.primary,
            selectbackground=DEFAULT_THEME.selection,
            selectforeground=DEFAULT_THEME.text,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=DEFAULT_THEME.border,
            highlightcolor=DEFAULT_THEME.focus,
            padx=10,
            pady=8,
        )
        json_vertical = ttk.Scrollbar(
            advanced,
            orient="vertical",
            command=self._model_json.yview,
            style="Settings.Vertical.TScrollbar",
        )
        json_horizontal = ttk.Scrollbar(
            advanced,
            orient="horizontal",
            command=self._model_json.xview,
            style="Settings.Horizontal.TScrollbar",
        )
        self._model_json.configure(
            yscrollcommand=json_vertical.set,
            xscrollcommand=json_horizontal.set,
        )
        self._model_json.grid(row=0, column=0, sticky="nsew")
        json_vertical.grid(row=0, column=1, sticky="ns")
        json_horizontal.grid(row=1, column=0, sticky="ew")
        editor_buttons = ttk.Frame(advanced, style="SettingsSurface.TFrame")
        editor_buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(
            editor_buttons,
            text="重新载入",
            command=lambda: self.refresh(reload_json=True),
            style="SettingsQuiet.TButton",
        ).pack(side="left")
        ttk.Button(
            editor_buttons,
            text="校验并保存 JSON",
            command=self._save_model_json,
            style="SettingsPrimary.TButton",
        ).pack(side="left", padx=(8, 0))
        self._advanced_section.grid_remove()

    def _build_credential_tab(self) -> None:
        tab = self._credential_page
        tab.columnconfigure(0, weight=1)
        self._build_tab_intro(
            tab,
            row=0,
            step="3",
            title="API Key",
            description=(
                "凭据仅用于调用阿里云 DashScope。已保存的密钥不会在界面或日志中回显。"
            ),
        )
        self._credential_card = ttk.Frame(
            tab,
            style="SettingsSoft.TFrame",
            padding=(16, 14),
        )
        self._credential_card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self._credential_card.columnconfigure(1, weight=1)
        self._credential_card_title = ttk.Label(
            self._credential_card,
            text="凭据状态",
            style="SettingsSoftTitle.TLabel",
        )
        self._credential_card_title.grid(row=0, column=0, sticky="w")
        self._credential_status = ttk.Label(
            self._credential_card,
            style="SettingsCredentialMuted.TLabel",
        )
        self._credential_status.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self._credential_storage_note = ttk.Label(
            self._credential_card,
            text="保存后立即清空输入框；应用不会读取并展示完整密钥。",
            style="SettingsSoftMuted.TLabel",
            wraplength=240,
            justify="left",
        )
        self._credential_storage_note.grid(
            row=1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(4, 0),
        )
        self._bind_responsive_wrap(
            self._credential_storage_note,
            self._credential_card,
        )

        self._credential_section, credential_body = self._section(
            tab,
            row=2,
            title="保存新的密钥",
            description="粘贴 DashScope API Key 后保存。新密钥会覆盖当前凭据。",
            pady=(0, 14),
        )
        credential_row, credential_label = self._form_container(
            credential_body,
            0,
            "新的 API Key",
        )
        self._credential_entry = ttk.Entry(
            credential_row,
            show="●",
            style="Settings.TEntry",
        )
        self._credential_row = self._register_form_row(
            credential_row,
            credential_label,
            self._credential_entry,
        )
        credential_note = ttk.Label(
            credential_body,
            text="密钥通常以 sk- 开头。请勿将它写入截图、日志或 Git 仓库。",
            style="SettingsSoftMuted.TLabel",
            wraplength=240,
            justify="left",
        )
        credential_note.grid(row=1, column=0, sticky="ew", pady=(4, 10))
        self._bind_responsive_wrap(credential_note, credential_body)
        self._save_api_key_button = ttk.Button(
            credential_body,
            text="保存 API Key",
            command=self._save_api_key,
            style="SettingsPrimary.TButton",
        )
        self._save_api_key_button.grid(row=2, column=0, sticky="e")

        self._danger_zone = ttk.Frame(
            tab,
            style="SettingsDangerZone.TFrame",
            padding=(14, 12),
        )
        self._danger_zone.grid(row=3, column=0, sticky="ew")
        self._danger_zone.columnconfigure(0, weight=1)
        ttk.Label(
            self._danger_zone,
            text="危险操作",
            style="SettingsDangerTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        danger_copy = ttk.Label(
            self._danger_zone,
            text="删除后，新的索引、搜索和标注请求将无法调用模型，直到重新保存密钥。",
            style="SettingsDangerCopy.TLabel",
            wraplength=240,
            justify="left",
        )
        danger_copy.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self._bind_responsive_wrap(danger_copy, self._danger_zone, reserved=170)
        self._delete_api_key_button = ttk.Button(
            self._danger_zone,
            text="删除已保存密钥",
            command=self._delete_api_key,
            style="SettingsDanger.TButton",
        )
        self._delete_api_key_button.grid(
            row=0,
            column=1,
            rowspan=2,
            sticky="e",
            padx=(14, 0),
        )

    def _path_row(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Callable[[], None],
    ) -> ttk.Button:
        container, label_widget = self._form_container(parent, row, label)
        field = ttk.Entry(
            container,
            textvariable=variable,
            state="readonly",
            style="Settings.TEntry",
        )
        button = ttk.Button(
            container,
            text="打开文件",
            command=command,
            style="SettingsQuiet.TButton",
        )
        self._register_form_row(container, label_widget, field, button)
        return button

    def _directory_row(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        directory: bool = True,
    ) -> None:
        container, label_widget = self._form_container(parent, row, label)
        field = ttk.Entry(
            container,
            textvariable=variable,
            style="Settings.TEntry",
        )
        button: ttk.Button | None = None
        if directory:
            button = ttk.Button(
                container,
                text="选择…",
                command=partial(self._choose_directory, variable),
                style="SettingsQuiet.TButton",
            )
        self._register_form_row(container, label_widget, field, button)

    def _model_row(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
        variable: tk.StringVar,
    ) -> ttk.Combobox:
        container, label_widget = self._form_container(parent, row, label)
        box = ttk.Combobox(
            container,
            textvariable=variable,
            state="readonly",
            style="Settings.TCombobox",
        )
        self._register_form_row(container, label_widget, box)
        return box

    def _form_container(
        self,
        parent: tk.Misc,
        row: int,
        label: str,
    ) -> tuple[ttk.Frame, ttk.Label]:
        container = ttk.Frame(parent, style="SettingsSoft.TFrame")
        container.grid(row=row, column=0, sticky="ew", pady=3)
        label_widget = ttk.Label(
            container,
            text=label,
            style="SettingsSoftLabel.TLabel",
        )
        return container, label_widget

    def _register_form_row(
        self,
        container: ttk.Frame,
        label: ttk.Label,
        field: tk.Widget,
        action: ttk.Button | None = None,
    ) -> _ResponsiveFormRow:
        row = _ResponsiveFormRow(container, label, field, action)
        self._responsive_rows.append(row)

        def reflow(event: tk.Event[tk.Misc], item: _ResponsiveFormRow = row) -> None:
            self._layout_form_row(item, event.width)

        container.bind(
            "<Configure>",
            reflow,
            add=True,
        )
        self._layout_form_row(row, self._FORM_COMPACT_WIDTH + 1)
        return row

    def _layout_form_row(self, row: _ResponsiveFormRow, width: int) -> None:
        """Reflow labels above fields when the available width is narrow."""

        compact = width < self._FORM_COMPACT_WIDTH
        if row.compact == compact:
            return
        row.compact = compact
        for widget in (row.label, row.field, row.action):
            if widget is not None:
                widget.grid_forget()
        if compact:
            row.container.columnconfigure(0, weight=1, minsize=0)
            row.container.columnconfigure(1, weight=0, minsize=0)
            row.container.columnconfigure(2, weight=0, minsize=0)
            row.label.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
            row.field.grid(
                row=1,
                column=0,
                columnspan=1 if row.action is not None else 2,
                sticky="ew",
                padx=(0, 8) if row.action is not None else 0,
            )
            if row.action is not None:
                row.action.grid(row=1, column=1, sticky="e")
            return
        row.container.columnconfigure(0, weight=0, minsize=112)
        row.container.columnconfigure(1, weight=1, minsize=0)
        row.container.columnconfigure(2, weight=0, minsize=0)
        row.label.grid(row=0, column=0, sticky="w", padx=(0, 10))
        row.field.grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(0, 8) if row.action is not None else 0,
        )
        if row.action is not None:
            row.action.grid(row=0, column=2, sticky="e")

    def _toggle_add_library_form(self) -> None:
        self._add_library_visible = not self._add_library_visible
        if self._add_library_visible:
            self._add_library_form.grid()
            self._add_library_toggle_button.configure(text="收起图库信息")
            return
        self._add_library_form.grid_remove()
        self._add_library_toggle_button.configure(text="填写图库信息")

    def _toggle_advanced_editor(self) -> None:
        self._advanced_visible = not self._advanced_visible
        if self._advanced_visible:
            self._advanced_section.grid()
            self._model_page.rowconfigure(4, weight=1)
            self._advanced_toggle_button.configure(text="收起 JSON 编辑器")
            self._model_json.focus_set()
            return
        self._advanced_section.grid_remove()
        self._model_page.rowconfigure(4, weight=0)
        self._advanced_toggle_button.configure(text="展开 JSON 编辑器")

    def _on_library_selection_changed(
        self,
        _event: tk.Event[tk.Misc] | None = None,
    ) -> None:
        """Keep library actions explicit and prevent invalid last-disable clicks."""

        selected = self._library_tree.selection()
        snapshot = self.settings_state.configuration
        if len(selected) != 1 or snapshot is None:
            self._set_default_button.state(["disabled"])
            self._toggle_library_button.state(["disabled"])
            self._toggle_library_button.configure(text="调整启用状态")
            return
        selected_id = selected[0]
        library = next(
            (item for item in snapshot.libraries if item.library_id == selected_id),
            None,
        )
        if library is None:
            self._set_default_button.state(["disabled"])
            self._toggle_library_button.state(["disabled"])
            return
        default_id = snapshot.configuration.default_library_id
        if selected_id == default_id:
            self._set_default_button.state(["disabled"])
        else:
            self._set_default_button.state(["!disabled"])
        self._toggle_library_button.configure(
            text="停用图库" if library.enabled else "启用图库"
        )
        enabled_count = sum(item.enabled for item in snapshot.libraries)
        cannot_disable_last = library.enabled and enabled_count <= 1
        self._toggle_library_button.state(
            ["disabled"] if cannot_disable_last else ["!disabled"]
        )

    def _apply_state(self, state: SettingsState, *, reload_json: bool) -> None:
        self._state = state
        self._config_path_var.set(str(self.controller.config_path))
        self._models_path_var.set(str(self.controller.models_path))
        self._config_open_button.state(
            ["!disabled"] if self.controller.config_path.is_file() else ["disabled"]
        )
        self._models_open_button.state(
            ["!disabled"] if self.controller.models_path.is_file() else ["disabled"]
        )
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
            self._library_tree.configure(height=3)
            self._library_summary.configure(text="尚未配置图库")
            self._on_library_selection_changed()
            return
        self._library_tree.configure(
            height=max(3, min(6, len(snapshot.libraries))),
        )
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
        enabled_count = sum(library.enabled for library in snapshot.libraries)
        self._library_summary.configure(
            text=f"共 {len(snapshot.libraries)} 个图库 · {enabled_count} 个已启用"
        )
        if default_id and self._library_tree.exists(default_id):
            self._library_tree.selection_set(default_id)
            self._library_tree.focus(default_id)
            self._library_tree.see(default_id)
        self._on_library_selection_changed()

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
            style = "SettingsSuccess.TLabel"
            self._credential_card.configure(style="SettingsSuccessCard.TFrame")
            self._credential_card_title.configure(style="SettingsSuccessTitle.TLabel")
            self._credential_storage_note.configure(style="SettingsSuccessMuted.TLabel")
            self._delete_api_key_button.state(["!disabled"])
        else:
            text = "尚未保存"
            style = "SettingsCredentialMuted.TLabel"
            self._credential_card.configure(style="SettingsSoft.TFrame")
            self._credential_card_title.configure(style="SettingsSoftTitle.TLabel")
            self._credential_storage_note.configure(style="SettingsSoftMuted.TLabel")
            self._delete_api_key_button.state(["disabled"])
        self._credential_status.configure(text=text, style=style)

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
            embedded=False,
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
