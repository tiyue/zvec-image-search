"""Pure-Python tkinter desktop for Zvec search and library tasks."""

from __future__ import annotations

import copy
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
import uuid
import weakref
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont
from typing import Any, cast

from PIL import Image, ImageTk

from .app import DesktopLaunchOptions
from .backend_host import BackendHost, BackendRuntime
from .configuration_service import DesktopConfigurationService
from .credentials import CredentialStore, default_credential_store
from .image_loader import BoundedImageLoader, ImageLoadResult
from .library_task_ui_state import (
    LibraryTaskAction,
    LibraryTaskFormValues,
    TaskCenterModel,
    build_library_task_request,
    project_task,
    resolve_failure_directory,
    task_is_terminal,
    task_result_text,
)
from .library_tasks import (
    DEFAULT_AUTO_TAG_BUDGET_CNY,
    DEFAULT_AUTO_TAG_MAX_IMAGES,
    DEFAULT_AUTO_TAG_MODEL,
    LibraryTaskOutcome,
    LibraryTaskService,
    LibraryTaskValidationError,
    SubmittedLibraryTask,
)
from .model_settings import ModelSettingsService
from .organize_panel import OrganizePanel
from .organize_runtime import RuntimeOrganizeOperations
from .resources import (
    apply_application_icon,
    configure_tk_scaling,
    resolve_ui_font_family,
)
from .result_catalog import (
    CatalogConfig,
    LibraryRecord,
    ResultCatalog,
    ResultCatalogError,
    SearchResult,
    SearchResultPage,
)
from .runtime_controller import (
    BackendRuntimeController,
    RuntimeErrorCategory,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeOperationError,
    RuntimeState,
)
from .search_service import (
    SearchOutcome,
    SearchRequest,
    SearchService,
    SearchValidationError,
)
from .search_ui_state import (
    SEARCH_MODE_LABELS,
    SEMANTIC_MODE_LABEL,
    TAG_MODE_LABEL,
    SearchFormValues,
    build_search_request,
    format_search_progress,
    outcome_error_text,
)
from .settings_panel import SettingsPanel
from .theme import DEFAULT_THEME, DesktopTheme
from .tray import TrayEvent, TrayEventKind, TrayIconError, TrayIconService
from .widgets import (
    AsyncImageCanvas,
    ImageTaskDispatcher,
    ResponsiveGallery,
)

BackgroundCompletion = Callable[[Any | None, BaseException | None], None]
SearchServiceFactory = Callable[[BackendRuntime], SearchService]
LibraryTaskServiceFactory = Callable[[BackendRuntime], LibraryTaskService]


@dataclass(frozen=True, slots=True)
class _OwnedTaskStarted:
    local_id: str
    submission: SubmittedLibraryTask
    cancel_event: threading.Event


@dataclass(frozen=True, slots=True)
class _LibraryTaskExecution:
    local_id: str
    submission: SubmittedLibraryTask | None
    outcome: LibraryTaskOutcome | None
    error: BaseException | None


class ZvecDesktopWindow:
    """Main pure-Python desktop window with an asynchronous search workflow."""

    def __init__(
        self,
        options: DesktopLaunchOptions,
        *,
        theme: DesktopTheme = DEFAULT_THEME,
        backend_host: BackendHost | None = None,
        runtime_controller: BackendRuntimeController | None = None,
        search_service_factory: SearchServiceFactory | None = None,
        library_task_service_factory: LibraryTaskServiceFactory | None = None,
        credential_store: CredentialStore | None = None,
        tray_service: TrayIconService | None = None,
        autostart_backend: bool = True,
    ) -> None:
        self.options = options
        self.root = tk.Tk(className="ZvecDesktop")
        configure_tk_scaling(self.root)
        resolved_font = resolve_ui_font_family(self.root, theme.typography)
        if resolved_font != theme.typography.family:
            theme = replace(
                theme,
                typography=replace(theme.typography, family=resolved_font),
            )
        self.theme = theme
        self.root.title("Zvec 图片库")
        self._application_icon = apply_application_icon(self.root)
        screen_width = max(720, self.root.winfo_screenwidth())
        screen_height = max(500, self.root.winfo_screenheight())
        initial_width = min(1280, max(720, screen_width - 80))
        initial_height = min(900, max(500, screen_height - 120))
        self.root.geometry(f"{initial_width}x{initial_height}")
        self.root.minsize(720, 500)
        self.root.configure(background=theme.window)
        self._configure_fonts()
        self._configure_styles()

        self._catalog: ResultCatalog | None = None
        self._page: SearchResultPage | None = None
        self._items: tuple[SearchResult, ...] = ()
        self._selected_index: int | None = None
        self._source_kind = "latest"
        self._source_path: Path | None = None
        self._load_generation = 0
        self._load_cancel = threading.Event()
        self._busy = False
        self._state_lock = threading.Lock()
        self._closed = False
        self._close_pending = False
        self._ui_thread_id = threading.get_ident()

        self._backend_host = backend_host or BackendHost(options.config_path)
        self._runtime_controller = runtime_controller or BackendRuntimeController(
            self._backend_host
        )
        self._search_service_factory = search_service_factory or (
            lambda runtime: SearchService(
                self._backend_host.client,
                runtime.query_root,
            )
        )
        self._library_task_service_factory = library_task_service_factory or (
            lambda _runtime: LibraryTaskService(self._backend_host.client)
        )
        self._credential_store = credential_store or default_credential_store()
        config_home = (
            options.config_path.expanduser().absolute().parent.resolve()
            if options.config_path is not None
            else self._backend_host.config_home
        )
        self._configuration_service = DesktopConfigurationService(
            options.config_path,
            config_home=config_home,
        )
        self._model_settings_service = ModelSettingsService(config_home / "models.json")
        self._organize_operations = RuntimeOrganizeOperations(self._runtime_controller)
        self._tray_service = tray_service
        self._tray_poll: str | None = None
        self._restart_pending = False
        self._restart_reason = ""
        self._restart_retry_poll: str | None = None
        self._restart_wait_notified = False
        self._restart_generation = 0
        self._restart_start_generation = 0
        self._organize_loaded = False
        self._organize_panel: OrganizePanel | None = None
        self._settings_panel: SettingsPanel | None = None
        self._saved_credential_loaded = False
        self._credentials_configured = False
        self._search_service: SearchService | None = None
        self._library_task_service: LibraryTaskService | None = None
        self._search_running = False
        self._search_cancel = threading.Event()
        self._search_generation = 0
        self._search_progress: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._query_image_path: Path | None = None
        self._libraries: tuple[LibraryRecord, ...] = ()
        self._task_center = TaskCenterModel()
        self._task_updates: queue.SimpleQueue[_OwnedTaskStarted | dict[str, Any]] = (
            queue.SimpleQueue()
        )
        self._active_task_cancels: dict[str, threading.Event] = {}
        self._task_refresh_future: Future[dict[str, Any]] | None = None
        self._next_task_refresh_at = 0.0
        self._task_results_directory: Path | None = None

        self._image_loader = BoundedImageLoader()
        self._image_dispatcher = ImageTaskDispatcher(self.root, self._load_fitted_image)
        self._background = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="zvec-desktop"
        )
        self._background_results: queue.SimpleQueue[
            tuple[int, Any | None, BaseException | None]
        ] = queue.SimpleQueue()
        self._next_completion_id = 0
        self._pending_completions: dict[int, BackgroundCompletion] = {}
        self._background_poll: str | None = self.root.after(35, self._drain_background)

        self._build_window()
        self._load_library_choices()
        self.root.protocol("WM_DELETE_WINDOW", self._handle_window_close)
        if self._tray_service is not None:
            self.root.after(0, self._start_tray)
        self.root.after(50, self._load_initial_source)
        if autostart_backend:
            self.root.after(80, self.start_backend)

    def run(self) -> None:
        self.root.mainloop()

    def show_window(self) -> None:
        """Restore and focus the main window from tray or single-instance events."""

        self._assert_ui_thread()
        if self._closed:
            return
        with suppress(tk.TclError):
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()

    def _handle_window_close(self) -> None:
        """Hide ordinary title-bar closes; explicit Exit performs shutdown."""

        tray = self._tray_service
        if tray is None or not tray.running:
            self.close()
            return
        if self._close_pending or self._closed:
            return
        with suppress(tk.TclError):
            self.root.withdraw()
        with suppress(TrayIconError):
            tray.notify("窗口已隐藏；后台任务会继续运行。", title="Zvec 图片库")

    def _start_tray(self) -> None:
        tray = self._tray_service
        if tray is None or self._closed:
            return
        try:
            tray.start()
        except TrayIconError as exc:
            self._tray_service = None
            self._status_text.set(f"系统托盘不可用：{exc}")
            return
        self._tray_poll = self.root.after(100, self._drain_tray_events)

    def _drain_tray_events(self) -> None:
        self._tray_poll = None
        tray = self._tray_service
        if tray is None or self._closed:
            return
        tray.drain_events(self._handle_tray_event)
        with suppress(tk.TclError):
            self._tray_poll = self.root.after(100, self._drain_tray_events)

    def _handle_tray_event(self, event: TrayEvent) -> None:
        self._assert_ui_thread()
        if event.kind is TrayEventKind.SHOW:
            self.show_window()
            return
        if event.kind is TrayEventKind.EXIT:
            with suppress(tk.TclError):
                self.root.deiconify()
            self.close()

    def close(self) -> None:
        """Request a safe backend shutdown before destroying the window.

        A busy backend deliberately keeps the window open.  This avoids turning
        the title-bar close button into an accidental “kill my import/search”
        operation while still allowing an idle application to exit normally.
        """

        with self._state_lock:
            if self._closed or self._close_pending:
                return
            self._close_pending = True
        self._load_generation += 1
        self._load_cancel.set()
        self._status_text.set("正在安全关闭后台…")
        future = self._runtime_controller.dispose()
        self._watch_future(future, self._complete_close_request)

    def _complete_close_request(
        self,
        _value: Any | None,
        error: BaseException | None,
    ) -> None:
        self._assert_ui_thread()
        if error is not None:
            with self._state_lock:
                self._close_pending = False
            if (
                isinstance(error, RuntimeOperationError)
                and error.info.category is RuntimeErrorCategory.BUSY
            ):
                with suppress(tk.TclError):
                    self.root.deiconify()
                    self.root.lift()
                details = error.info.details
                active_ids = (
                    details.get("active_job_ids", [])
                    if isinstance(details, dict)
                    else []
                )
                suffix = f"（任务：{', '.join(active_ids)}）" if active_ids else ""
                message = (
                    "后台任务仍在运行，窗口已保持打开，不会终止任务。"
                    f"{suffix} 请等待完成或先取消任务后再退出。"
                )
                self._status_text.set(message)
                messagebox.showwarning("后台仍在工作", message, parent=self.root)
                return
            message = f"无法安全关闭后台：{error}"
            self._status_text.set(message)
            messagebox.showerror("无法退出", message, parent=self.root)
            return
        self._finalize_close()

    def _finalize_close(self) -> None:
        self._assert_ui_thread()
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._close_pending = False
        self._search_generation += 1
        self._search_cancel.set()
        if self._tray_poll is not None:
            with suppress(tk.TclError):
                self.root.after_cancel(self._tray_poll)
            self._tray_poll = None
        if self._restart_retry_poll is not None:
            with suppress(tk.TclError):
                self.root.after_cancel(self._restart_retry_poll)
            self._restart_retry_poll = None
        if self._background_poll is not None:
            with suppress(tk.TclError):
                self.root.after_cancel(self._background_poll)
            self._background_poll = None
        # Do not wait on filesystem or image I/O from Tk's owner thread.  Active
        # folder scans observe ``_load_cancel`` and late Future callbacks see
        # ``_closed`` before attempting to enqueue a tkinter completion.
        self._background.shutdown(wait=False, cancel_futures=True)
        while True:
            try:
                self._background_results.get_nowait()
            except queue.Empty:
                break
        # Completion closures commonly reference StringVar/widget instances.
        # Release them here on Tk's owner thread, never from a Future worker.
        self._pending_completions.clear()
        self._active_task_cancels.clear()
        with suppress(tk.TclError):
            self._task_progress.stop()
        if self._organize_panel is not None:
            with suppress(tk.TclError):
                self._organize_panel.close()
        if self._tray_service is not None:
            with suppress(TrayIconError):
                self._tray_service.stop()
        self._image_dispatcher.close()
        self._image_loader.close()
        with suppress(tk.TclError):
            self.root.destroy()

    def _configure_fonts(self) -> None:
        """Configure Tk named fonts without overriding ttk style hierarchy."""

        for name, size in (
            ("TkDefaultFont", self.theme.typography.body),
            ("TkTextFont", self.theme.typography.body),
            ("TkMenuFont", self.theme.typography.body),
            ("TkCaptionFont", self.theme.typography.body),
            ("TkSmallCaptionFont", self.theme.typography.supporting),
            ("TkIconFont", self.theme.typography.body),
            ("TkTooltipFont", self.theme.typography.supporting),
        ):
            with suppress(tk.TclError):
                tkfont.nametofont(name, root=self.root).configure(
                    family=self.theme.typography.family,
                    size=size,
                )

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        with suppress(tk.TclError):
            style.theme_use("clam")
        font = self.theme.typography.family
        style.configure(
            ".",
            background=self.theme.window,
            foreground=self.theme.text,
            font=(font, self.theme.typography.body),
        )
        style.configure("App.TFrame", background=self.theme.window)
        style.configure("Header.TFrame", background=self.theme.surface)
        style.configure("Surface.TFrame", background=self.theme.surface)
        style.configure("Subtle.TFrame", background=self.theme.surface_subtle)
        style.configure(
            "Hero.TFrame",
            background=self.theme.surface,
            bordercolor=self.theme.primary_soft,
            lightcolor=self.theme.primary_soft,
            darkcolor=self.theme.primary_soft,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Card.TFrame",
            background=self.theme.surface,
            bordercolor=self.theme.border,
            lightcolor=self.theme.border,
            darkcolor=self.theme.border,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "StatusPill.TFrame",
            background=self.theme.surface_subtle,
            bordercolor=self.theme.border,
            lightcolor=self.theme.border,
            darkcolor=self.theme.border,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "Navigation.TFrame",
            background=self.theme.navigation,
            bordercolor=self.theme.navigation,
            borderwidth=0,
        )
        style.configure(
            "StatusBar.TFrame",
            background=self.theme.surface,
            bordercolor=self.theme.border,
            borderwidth=1,
            relief="flat",
        )
        style.configure(
            "Title.TLabel",
            background=self.theme.window,
            foreground=self.theme.text,
            font=(font, self.theme.typography.title, "bold"),
        )
        style.configure(
            "BrandSubtitle.TLabel",
            background=self.theme.window,
            foreground=self.theme.text_muted,
            font=(font, self.theme.typography.body),
        )
        style.configure(
            "HeaderTitle.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text,
            font=(font, self.theme.typography.page_title, "bold"),
        )
        style.configure(
            "HeaderSubtitle.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text_muted,
            font=(font, self.theme.typography.body),
        )
        style.configure(
            "SidebarTitle.TLabel",
            background=self.theme.navigation,
            foreground="#FFFFFF",
            font=(font, self.theme.typography.section, "bold"),
        )
        style.configure(
            "SidebarSubtitle.TLabel",
            background=self.theme.navigation,
            foreground=self.theme.navigation_muted,
            font=(font, self.theme.typography.supporting),
        )
        style.configure(
            "Section.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text,
            font=(font, self.theme.typography.section, "bold"),
        )
        style.configure(
            "Eyebrow.TLabel",
            background=self.theme.surface,
            foreground=self.theme.primary,
            font=(font, self.theme.typography.supporting, "bold"),
        )
        style.configure(
            "SubtleSection.TLabel",
            background=self.theme.surface_subtle,
            foreground=self.theme.text,
            font=(font, self.theme.typography.control, "bold"),
        )
        style.configure(
            "SubtleMuted.TLabel",
            background=self.theme.surface_subtle,
            foreground=self.theme.text_muted,
            font=(font, self.theme.typography.supporting),
        )
        style.configure(
            "Badge.TLabel",
            background=self.theme.primary_soft,
            foreground=self.theme.primary_pressed,
            font=(font, self.theme.typography.supporting, "bold"),
            padding=(8, 4),
        )
        style.configure(
            "SuccessBadge.TLabel",
            background=self.theme.success_soft,
            foreground=self.theme.success,
            font=(font, self.theme.typography.supporting, "bold"),
            padding=(8, 4),
        )
        style.configure(
            "EmptyTitle.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text,
            font=(font, self.theme.typography.section, "bold"),
        )
        style.configure(
            "EmptyHint.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text_muted,
            font=(font, self.theme.typography.body),
        )
        style.configure(
            "PreviewTitle.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text,
            font=(font, self.theme.typography.preview_title, "bold"),
        )
        style.configure(
            "Muted.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text_muted,
            font=(font, self.theme.typography.body),
        )
        style.configure(
            "Hint.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text_faint,
            font=(font, self.theme.typography.supporting),
        )
        style.configure(
            "ErrorHint.TLabel",
            background=self.theme.surface,
            foreground=self.theme.danger,
            font=(font, self.theme.typography.supporting),
        )
        style.configure(
            "NavHeading.TLabel",
            background=self.theme.navigation,
            foreground=self.theme.navigation_muted,
            font=(font, self.theme.typography.supporting, "bold"),
        )
        style.configure(
            "StatusMuted.TLabel",
            background=self.theme.surface_subtle,
            foreground=self.theme.text_muted,
            font=(font, self.theme.typography.body, "bold"),
        )
        style.configure(
            "StatusStarting.TLabel",
            background=self.theme.surface_subtle,
            foreground=self.theme.primary,
            font=(font, self.theme.typography.body, "bold"),
        )
        style.configure(
            "StatusSuccess.TLabel",
            background=self.theme.surface_subtle,
            foreground=self.theme.success,
            font=(font, self.theme.typography.body, "bold"),
        )
        style.configure(
            "StatusError.TLabel",
            background=self.theme.surface_subtle,
            foreground=self.theme.danger,
            font=(font, self.theme.typography.body, "bold"),
        )
        style.configure(
            "Primary.TButton",
            background=self.theme.primary,
            foreground="white",
            bordercolor=self.theme.primary,
            lightcolor=self.theme.primary,
            darkcolor=self.theme.primary,
            focuscolor=self.theme.primary,
            borderwidth=1,
            padding=(15, 9),
            font=(font, self.theme.typography.control, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[
                ("pressed", self.theme.primary_pressed),
                ("active", self.theme.primary_hover),
                ("disabled", self.theme.surface_strong),
            ],
            bordercolor=[
                ("pressed", self.theme.primary_pressed),
                ("active", self.theme.primary_hover),
                ("disabled", self.theme.surface_strong),
            ],
            foreground=[("disabled", self.theme.text_faint)],
        )
        style.configure(
            "Secondary.TButton",
            background=self.theme.primary_soft,
            foreground=self.theme.primary_pressed,
            bordercolor=self.theme.primary_soft,
            lightcolor=self.theme.primary_soft,
            darkcolor=self.theme.primary_soft,
            focuscolor=self.theme.primary_soft,
            borderwidth=1,
            padding=(13, 8),
            font=(font, self.theme.typography.control, "bold"),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", self.theme.selection)],
            bordercolor=[("active", self.theme.selection)],
            foreground=[("disabled", self.theme.text_faint)],
        )
        style.configure(
            "Quiet.TButton",
            background=self.theme.surface,
            foreground=self.theme.text,
            bordercolor=self.theme.border_strong,
            lightcolor=self.theme.border_strong,
            darkcolor=self.theme.border_strong,
            focuscolor=self.theme.border_strong,
            borderwidth=1,
            padding=(11, 8),
            font=(font, self.theme.typography.control),
        )
        style.map(
            "Quiet.TButton",
            background=[
                ("active", self.theme.surface_muted),
                ("disabled", self.theme.surface_subtle),
            ],
            foreground=[("disabled", self.theme.text_faint)],
        )
        style.configure(
            "Danger.TButton",
            background=self.theme.surface,
            foreground=self.theme.danger,
            bordercolor=self.theme.danger_soft,
            lightcolor=self.theme.danger_soft,
            darkcolor=self.theme.danger_soft,
            borderwidth=1,
            padding=(11, 8),
        )
        style.map(
            "Danger.TButton",
            background=[
                ("active", self.theme.danger_soft),
                ("disabled", self.theme.surface),
            ],
            foreground=[("disabled", self.theme.text_faint)],
            bordercolor=[("disabled", self.theme.border)],
            lightcolor=[("disabled", self.theme.border)],
            darkcolor=[("disabled", self.theme.border)],
        )
        style.configure(
            "Nav.TButton",
            anchor="w",
            background=self.theme.navigation,
            foreground=self.theme.navigation_text,
            bordercolor=self.theme.navigation,
            lightcolor=self.theme.navigation,
            darkcolor=self.theme.navigation,
            focuscolor=self.theme.navigation,
            borderwidth=0,
            padding=(13, 11),
            font=(font, self.theme.typography.control),
        )
        style.map(
            "Nav.TButton",
            background=[("active", self.theme.navigation_hover)],
            foreground=[("active", "#FFFFFF")],
        )
        style.configure(
            "NavSelected.TButton",
            anchor="w",
            background=self.theme.navigation_selected,
            foreground="#FFFFFF",
            bordercolor=self.theme.navigation_selected,
            lightcolor=self.theme.navigation_selected,
            darkcolor=self.theme.navigation_selected,
            focuscolor=self.theme.navigation_selected,
            borderwidth=0,
            padding=(13, 11),
            font=(font, self.theme.typography.control, "bold"),
        )
        style.map(
            "NavSelected.TButton",
            background=[("active", self.theme.primary)],
        )
        style.configure(
            "NavExit.TButton",
            anchor="w",
            background=self.theme.navigation,
            foreground=self.theme.navigation_muted,
            bordercolor=self.theme.navigation,
            lightcolor=self.theme.navigation,
            darkcolor=self.theme.navigation,
            focuscolor=self.theme.navigation,
            borderwidth=0,
            padding=(13, 10),
            font=(font, self.theme.typography.control),
        )
        style.map(
            "NavExit.TButton",
            background=[("active", self.theme.danger)],
            foreground=[("active", "#FFFFFF")],
        )
        # A modern default keeps the embedded organize/settings pages from
        # falling back to the platform's dated raised-button appearance.
        style.configure(
            "TButton",
            background=self.theme.surface,
            foreground=self.theme.text,
            bordercolor=self.theme.border_strong,
            lightcolor=self.theme.border_strong,
            darkcolor=self.theme.border_strong,
            focuscolor=self.theme.primary_soft,
            borderwidth=1,
            padding=(11, 7),
            font=(font, self.theme.typography.control),
        )
        style.map(
            "TButton",
            background=[
                ("pressed", self.theme.surface_strong),
                ("active", self.theme.surface_muted),
                ("disabled", self.theme.surface_subtle),
            ],
            foreground=[("disabled", self.theme.text_faint)],
            bordercolor=[("focus", self.theme.primary)],
        )
        style.configure(
            "TEntry",
            fieldbackground=self.theme.surface,
            foreground=self.theme.text,
            bordercolor=self.theme.border_strong,
            lightcolor=self.theme.border_strong,
            darkcolor=self.theme.border_strong,
            insertcolor=self.theme.primary,
            padding=(9, 7),
            borderwidth=1,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", self.theme.primary)],
            lightcolor=[("focus", self.theme.primary)],
            darkcolor=[("focus", self.theme.primary)],
        )
        style.configure(
            "Search.TEntry",
            padding=(12, 11),
            font=(font, 10),
        )
        style.configure(
            "TCombobox",
            fieldbackground=self.theme.surface,
            background=self.theme.surface,
            foreground=self.theme.text,
            arrowcolor=self.theme.text_muted,
            bordercolor=self.theme.border_strong,
            lightcolor=self.theme.border_strong,
            darkcolor=self.theme.border_strong,
            padding=(8, 7),
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", self.theme.surface)],
            bordercolor=[("focus", self.theme.primary)],
            lightcolor=[("focus", self.theme.primary)],
            darkcolor=[("focus", self.theme.primary)],
        )
        style.configure(
            "TSpinbox",
            fieldbackground=self.theme.surface,
            background=self.theme.surface,
            foreground=self.theme.text,
            arrowcolor=self.theme.text_muted,
            bordercolor=self.theme.border_strong,
            lightcolor=self.theme.border_strong,
            darkcolor=self.theme.border_strong,
            insertcolor=self.theme.primary,
            padding=(8, 7),
        )
        style.map(
            "TSpinbox",
            bordercolor=[("focus", self.theme.primary)],
            lightcolor=[("focus", self.theme.primary)],
            darkcolor=[("focus", self.theme.primary)],
        )
        style.configure(
            "TCheckbutton",
            background=self.theme.surface,
            foreground=self.theme.text_muted,
            indicatorbackground=self.theme.surface,
            indicatorforeground=self.theme.primary,
            padding=(2, 2),
        )
        style.map(
            "TCheckbutton",
            background=[("active", self.theme.surface)],
            foreground=[("active", self.theme.text)],
            indicatorbackground=[("selected", self.theme.primary)],
        )
        style.configure(
            "Subtle.TCheckbutton",
            background=self.theme.surface_subtle,
            foreground=self.theme.text_muted,
            indicatorbackground=self.theme.surface,
            indicatorforeground=self.theme.primary,
            padding=(2, 2),
        )
        style.map(
            "Subtle.TCheckbutton",
            background=[("active", self.theme.surface_subtle)],
            foreground=[("active", self.theme.text)],
            indicatorbackground=[("selected", self.theme.primary)],
        )
        style.configure(
            "Treeview",
            background=self.theme.surface,
            fieldbackground=self.theme.surface,
            foreground=self.theme.text,
            bordercolor=self.theme.border,
            rowheight=34,
        )
        style.map(
            "Treeview",
            background=[("selected", self.theme.selection)],
            foreground=[("selected", self.theme.text)],
        )
        style.configure(
            "Treeview.Heading",
            background=self.theme.surface_muted,
            foreground=self.theme.text_muted,
            bordercolor=self.theme.border,
            padding=(8, 7),
            font=(font, self.theme.typography.supporting, "bold"),
        )
        style.map(
            "Treeview.Heading", background=[("active", self.theme.surface_strong)]
        )
        style.configure(
            "TLabelframe",
            background=self.theme.surface,
            bordercolor=self.theme.border,
            lightcolor=self.theme.border,
            darkcolor=self.theme.border,
            borderwidth=1,
            relief="solid",
        )
        style.configure(
            "TLabelframe.Label",
            background=self.theme.surface,
            foreground=self.theme.text,
            font=(font, self.theme.typography.control, "bold"),
        )
        style.configure(
            "TNotebook",
            background=self.theme.surface,
            bordercolor=self.theme.border,
            borderwidth=0,
            tabmargins=(0, 0, 0, 8),
        )
        style.configure(
            "TNotebook.Tab",
            background=self.theme.surface_muted,
            foreground=self.theme.text_muted,
            borderwidth=0,
            padding=(14, 8),
            font=(font, self.theme.typography.control),
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", self.theme.primary_soft),
                ("active", self.theme.surface_strong),
            ],
            foreground=[("selected", self.theme.primary_pressed)],
        )
        style.configure(
            "Vertical.TScrollbar",
            background=self.theme.border_strong,
            troughcolor=self.theme.surface_subtle,
            bordercolor=self.theme.surface_subtle,
            arrowcolor=self.theme.text_muted,
            width=12,
        )
        style.configure(
            "Horizontal.TProgressbar",
            background=self.theme.primary,
            troughcolor=self.theme.surface_muted,
            bordercolor=self.theme.surface_muted,
            lightcolor=self.theme.primary,
            darkcolor=self.theme.primary,
            borderwidth=0,
            thickness=8,
        )
        style.configure(
            "Workspace.TPanedwindow",
            background=self.theme.window,
            sashwidth=10,
            sashpad=2,
        )
        style.configure(
            "Workspace.TNotebook",
            background=self.theme.window,
            borderwidth=0,
            tabmargins=0,
        )
        # Pages remain real Notebook tabs for keyboard/programmatic navigation,
        # but the platform tab strip is replaced by the modern left rail.
        style.layout("Workspace.TNotebook.Tab", [])

    def _build_window(self) -> None:
        shell = ttk.Frame(self.root, style="App.TFrame")
        shell.pack(fill="both", expand=True)
        shell.rowconfigure(0, weight=1)
        shell.columnconfigure(1, weight=1)

        # The navigation rail now owns the brand and spans the full window.
        # This removes the disconnected banner/sidebar composition and gives
        # every page one stable visual anchor.
        navigation = ttk.Frame(
            shell,
            style="Navigation.TFrame",
            padding=(14, 20, 14, 16),
            width=208,
        )
        navigation.grid(row=0, column=0, sticky="ns")
        navigation.grid_propagate(False)
        navigation.columnconfigure(0, weight=1)
        navigation.rowconfigure(8, weight=1)

        brand = ttk.Frame(navigation, style="Navigation.TFrame")
        brand.grid(row=0, column=0, sticky="ew", padx=7, pady=(0, 20))
        brand.columnconfigure(1, weight=1)
        self._navigation_logo_photo: ImageTk.PhotoImage | None = None
        if self._application_icon is not None:
            try:
                with Image.open(self._application_icon) as icon_source:
                    icon_image = icon_source.convert("RGBA").resize(
                        (36, 36),
                        Image.Resampling.LANCZOS,
                    )
                try:
                    self._navigation_logo_photo = ImageTk.PhotoImage(
                        icon_image,
                        master=self.root,
                    )
                finally:
                    icon_image.close()
            except (OSError, RuntimeError, tk.TclError, ValueError):
                # A damaged optional icon must never prevent the desktop from
                # opening; the compact lettermark remains a safe fallback.
                self._navigation_logo_photo = None
        if self._navigation_logo_photo is None:
            self._navigation_logo = tk.Label(
                brand,
                text="Z",
                width=2,
                height=1,
                background=self.theme.primary,
                foreground="#FFFFFF",
                font=(self.theme.typography.family, 15, "bold"),
                borderwidth=0,
            )
        else:
            self._navigation_logo = tk.Label(
                brand,
                image=self._navigation_logo_photo,
                background=self.theme.navigation,
                borderwidth=0,
            )
        self._navigation_logo.grid(row=0, column=0, rowspan=2, sticky="w")
        self._navigation_brand = ttk.Label(
            brand,
            text="Zvec",
            style="SidebarTitle.TLabel",
        )
        self._navigation_brand.grid(row=0, column=1, sticky="sw", padx=(10, 0))
        self._navigation_caption = ttk.Label(
            brand,
            text="智能图片库",
            style="SidebarSubtitle.TLabel",
        )
        self._navigation_caption.grid(
            row=1, column=1, sticky="nw", padx=(10, 0), pady=(1, 0)
        )
        tk.Frame(
            navigation,
            height=1,
            background=self.theme.navigation_hover,
            borderwidth=0,
        ).grid(row=1, column=0, sticky="ew", padx=7, pady=(0, 16))
        self._navigation_heading = ttk.Label(
            navigation,
            text="工作区",
            style="NavHeading.TLabel",
        )
        self._navigation_heading.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 8))

        self._nav_buttons: list[ttk.Button] = []
        self._nav_items = (
            ("⌕", "图片搜索", "语义、图片与标签"),
            ("▦", "图库任务", "索引、同步与标注"),
            ("✓", "智能整理", "标签审核与别名"),
            ("⚙", "设置", "图库、模型与密钥"),
        )
        self._nav_compact_labels = ("搜索", "任务", "整理", "设置")
        for row, (icon, label, hint) in enumerate(self._nav_items, start=3):
            button = ttk.Button(
                navigation,
                text=f"{icon}  {label}\n     {hint}",
                style="NavSelected.TButton" if row == 3 else "Nav.TButton",
                command=partial(self._select_main_page, row - 3),
            )
            button.grid(row=row, column=0, sticky="ew", pady=3)
            self._nav_buttons.append(button)
        self._navigation_footer = ttk.Label(
            navigation,
            text="本地运行\n隐私优先 · 无 Docker",
            style="NavHeading.TLabel",
            justify="left",
        )
        self._navigation_footer.grid(
            row=9, column=0, sticky="sw", padx=10, pady=(12, 4)
        )
        self._exit_button = ttk.Button(
            navigation,
            text="退出应用",
            style="NavExit.TButton",
            command=self.close,
        )
        self._exit_button.grid(row=10, column=0, sticky="ew", pady=(4, 0))

        content = ttk.Frame(shell, style="App.TFrame")
        content.grid(row=0, column=1, sticky="nsew")
        content.rowconfigure(1, weight=1)
        content.columnconfigure(0, weight=1)

        header = ttk.Frame(
            content,
            style="Header.TFrame",
            padding=(22, 12, 20, 11),
        )
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self._page_title_text = tk.StringVar(value="图片搜索")
        self._page_subtitle_text = tk.StringVar(value="正在读取图片…")
        self._source_text = tk.StringVar(value="正在读取图片…")
        page_heading = ttk.Frame(header, style="Header.TFrame")
        page_heading.grid(row=0, column=0, sticky="w")
        ttk.Label(
            page_heading,
            textvariable=self._page_title_text,
            style="HeaderTitle.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self._page_subtitle_label = ttk.Label(
            page_heading,
            textvariable=self._page_subtitle_text,
            style="HeaderSubtitle.TLabel",
        )
        self._page_subtitle_label.grid(row=1, column=0, sticky="w", pady=(2, 0))

        header_actions = ttk.Frame(header, style="Header.TFrame")
        header_actions.grid(row=0, column=1, sticky="e", padx=(16, 0))
        self._backend_status_text = tk.StringVar(value="等待启动")
        self._backend_status_pill = ttk.Frame(
            header_actions,
            style="StatusPill.TFrame",
            padding=(9, 5),
        )
        self._backend_status_pill.grid(row=0, column=0, padx=(0, 9))
        self._backend_status_dot = ttk.Label(
            self._backend_status_pill,
            text="●",
            style="StatusMuted.TLabel",
        )
        self._backend_status_dot.grid(row=0, column=0, padx=(0, 5))
        self._backend_status_label = ttk.Label(
            self._backend_status_pill,
            textvariable=self._backend_status_text,
            style="StatusMuted.TLabel",
        )
        self._backend_status_label.grid(row=0, column=1)
        self._retry_backend_button = ttk.Button(
            header_actions,
            text="重试",
            style="Quiet.TButton",
            command=self.start_backend,
        )
        self._retry_backend_button.grid(row=0, column=1, padx=(0, 12))
        self._retry_backend_button.grid_remove()
        self._header_search_actions = ttk.Frame(
            header_actions,
            style="Header.TFrame",
        )
        self._header_search_actions.grid(row=0, column=2)
        self._latest_button = ttk.Button(
            self._header_search_actions,
            text="最近结果",
            style="Secondary.TButton",
            command=self.load_latest,
        )
        self._latest_button.grid(row=0, column=0, padx=(0, 8))
        self._folder_button = ttk.Button(
            self._header_search_actions,
            text="打开图片",
            style="Primary.TButton",
            command=self.choose_folder,
        )
        self._folder_button.grid(row=0, column=1)

        self._notebook = ttk.Notebook(content, style="Workspace.TNotebook")
        self._notebook.grid(row=1, column=0, sticky="nsew")
        page_padding = (18, 11, 18, 10)
        search_page = ttk.Frame(
            self._notebook,
            style="App.TFrame",
            padding=page_padding,
        )
        task_page = ttk.Frame(
            self._notebook,
            style="App.TFrame",
            padding=page_padding,
        )
        self._organize_page = ttk.Frame(
            self._notebook,
            style="App.TFrame",
            padding=page_padding,
        )
        self._settings_page = ttk.Frame(
            self._notebook,
            style="App.TFrame",
            padding=page_padding,
        )
        self._notebook.add(search_page, text="图片搜索")
        self._notebook.add(task_page, text="图库任务")
        self._notebook.add(self._organize_page, text="智能整理")
        self._notebook.add(self._settings_page, text="设置")
        self._main_pages = (
            search_page,
            task_page,
            self._organize_page,
            self._settings_page,
        )
        self._page_header_items = (
            ("图片搜索", ""),
            ("图库任务", "批量索引、同步与自动标注"),
            ("智能整理", "快速审核模型建议、身份标签与别名"),
            ("设置", "管理图库路径、阿里云模型与安全凭据"),
        )
        self._page_status_hints = (
            "单击图片查看详情 · 右侧预览完整显示且不裁剪",
            "任务在后台运行 · 单图失败会跳过并统一汇总",
            "快捷键：A 接受 · E 编辑 · R 拒绝 · Space 批量勾选",
            "设置保存后会等待任务结束并安全重启后台",
        )
        self._navigation_compact = False

        def update_shell(event: tk.Event[tk.Misc]) -> None:
            compact = event.width < 900
            if compact == self._navigation_compact:
                return
            self._navigation_compact = compact
            navigation.configure(
                width=92 if compact else 184,
                padding=(6, 16, 6, 12) if compact else (12, 18, 12, 14),
            )
            for button, (icon, label, hint), compact_label in zip(
                self._nav_buttons,
                self._nav_items,
                self._nav_compact_labels,
                strict=True,
            ):
                button.configure(
                    text=(
                        f"{icon} {compact_label}"
                        if compact
                        else f"{icon}  {label}\n     {hint}"
                    )
                )
            if compact:
                self._navigation_caption.grid_remove()
                self._navigation_brand.grid_remove()
                brand.columnconfigure(0, weight=1)
                brand.columnconfigure(1, weight=0)
                self._navigation_logo.grid_configure(sticky="")
                self._navigation_heading.configure(text="导航")
                self._status_hint_label.grid_remove()
            else:
                self._navigation_caption.grid()
                self._navigation_brand.grid()
                brand.columnconfigure(0, weight=0)
                brand.columnconfigure(1, weight=1)
                self._navigation_logo.grid_configure(sticky="w")
                self._navigation_heading.configure(text="工作区")
                self._status_hint_label.grid()
            self._navigation_footer.configure(
                text="本地运行" if compact else "本地运行\n隐私优先 · 无 Docker"
            )
            self._exit_button.configure(text="退出" if compact else "退出应用")

        self._notebook.bind("<<NotebookTabChanged>>", self._on_main_tab_changed)
        search_page.rowconfigure(1, weight=1)
        search_page.columnconfigure(0, weight=1)
        self._build_search(search_page)

        body = ttk.Frame(search_page, style="App.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=5, uniform="search-panes")
        body.columnconfigure(1, weight=3, uniform="search-panes")
        gallery_card = ttk.Frame(body, style="Card.TFrame", padding=16)
        preview_card = ttk.Frame(body, style="Card.TFrame", padding=16)
        gallery_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        preview_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        self._search_preview_collapsed = False

        def update_search_workspace(event: tk.Event[tk.Misc]) -> None:
            # Below 1000 logical pixels the side preview makes both panes too
            # narrow. The gallery takes the workspace until the user widens
            # the window enough to restore the complete-image preview pane.
            collapsed = event.width < 1000
            if collapsed == self._search_preview_collapsed:
                return
            self._search_preview_collapsed = collapsed
            if collapsed:
                preview_card.grid_remove()
                gallery_card.grid_configure(columnspan=2, padx=0)
            else:
                gallery_card.grid_configure(columnspan=1, padx=(0, 7))
                preview_card.grid()

        body.bind("<Configure>", update_search_workspace, add=True)
        self._build_gallery(gallery_card)
        self._build_preview(preview_card)
        self._build_library_tasks(task_page)
        self._build_organize_page(self._organize_page)
        self._build_settings_page(self._settings_page)

        status_frame = ttk.Frame(
            content,
            style="StatusBar.TFrame",
            padding=(18, 6, 18, 6),
        )
        status_frame.grid(row=2, column=0, sticky="ew")
        status_frame.columnconfigure(1, weight=1)
        ttk.Label(
            status_frame,
            text="●",
            background=self.theme.surface,
            foreground=self.theme.success,
        ).grid(row=0, column=0, sticky="w", padx=(0, 7))
        self._status_text = tk.StringVar(value="准备就绪")
        ttk.Label(
            status_frame,
            textvariable=self._status_text,
            background=self.theme.surface,
            foreground=self.theme.text_muted,
        ).grid(row=0, column=1, sticky="w")
        self._status_hint_label = ttk.Label(
            status_frame,
            text="单击图片查看详情 · 右侧预览完整显示且不裁剪",
            background=self.theme.surface,
            foreground=self.theme.text_muted,
        )
        self._status_hint_label.grid(row=0, column=2, sticky="e")
        shell.bind("<Configure>", update_shell, add=True)
        self._on_main_tab_changed()

    def _build_organize_page(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(parent, style="Surface.TFrame", padding=(12, 10))
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="当前图库", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        self._organize_library_var = tk.StringVar(value="")
        self._organize_library_combo = ttk.Combobox(
            toolbar,
            textvariable=self._organize_library_var,
            state="readonly",
            width=28,
        )
        self._organize_library_combo.grid(
            row=0,
            column=1,
            sticky="ew",
            padx=(0, 12),
        )
        self._organize_library_combo.bind(
            "<<ComboboxSelected>>", self._on_organize_library_selected
        )
        self._organize_toolbar_hint = ttk.Label(
            toolbar,
            text="支持本次新增、角色、作品、动作、神态和待审核状态筛选",
            style="Muted.TLabel",
        )
        self._organize_toolbar_hint.grid(row=0, column=2, sticky="e")

        def update_toolbar(event: tk.Event[tk.Misc]) -> None:
            if event.width < 760:
                self._organize_toolbar_hint.grid_remove()
            else:
                self._organize_toolbar_hint.grid()

        toolbar.bind("<Configure>", update_toolbar, add=True)

        self._organize_panel = OrganizePanel(
            parent,
            library_id="unconfigured",
            operations=self._organize_operations,
            image_dispatcher=self._image_dispatcher,
            theme=self.theme,
            on_open_image=self._open_path,
            auto_load=False,
        )
        self._organize_panel.grid(row=1, column=0, sticky="nsew")

    def _build_settings_page(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        self._settings_panel = SettingsPanel(
            parent,
            self._configuration_service,
            self._model_settings_service,
            self._credential_store,
            path_opener=self._open_path,
            on_restart_required=self._request_backend_restart,
            on_credentials_changed=self._on_settings_credentials_changed,
        )
        self._settings_panel.grid(row=0, column=0, sticky="nsew")

    def _select_main_page(self, index: int) -> None:
        """Switch the hidden notebook through the visible navigation rail."""

        if index < 0 or index >= len(self._main_pages):
            return
        self._notebook.select(self._main_pages[index])

    def _on_main_tab_changed(self, _event: tk.Event[Any] | None = None) -> None:
        self._assert_ui_thread()
        selected = self._notebook.select()
        selected_index = 0
        for index, button in enumerate(self._nav_buttons):
            is_selected = selected == str(self._main_pages[index])
            button.configure(
                style="NavSelected.TButton" if is_selected else "Nav.TButton"
            )
            if is_selected:
                selected_index = index

        title, subtitle = self._page_header_items[selected_index]
        self._page_title_text.set(title)
        self._page_subtitle_text.set(
            self._source_text.get() if selected_index == 0 else subtitle
        )
        self._status_hint_label.configure(text=self._page_status_hints[selected_index])
        if selected_index == 0:
            self._header_search_actions.grid()
        else:
            self._header_search_actions.grid_remove()

        if selected == str(self._settings_page):
            if self._settings_panel is not None:
                self._settings_panel.refresh(reload_json=False)
            return
        if selected == str(self._organize_page):
            self._ensure_organize_loaded()

    def _on_organize_library_selected(
        self, _event: tk.Event[Any] | None = None
    ) -> None:
        self._assert_ui_thread()
        library_id = self._selected_organize_library_id()
        panel = self._organize_panel
        if not library_id or panel is None:
            return
        self._organize_loaded = False
        panel.set_library(
            library_id,
            refresh=self._runtime_controller.is_ready,
        )
        self._organize_loaded = self._runtime_controller.is_ready

    def _selected_organize_library_id(self) -> str:
        index = self._organize_library_combo.current()
        if 0 <= index < len(self._libraries):
            return self._libraries[index].library_id
        return ""

    def _ensure_organize_loaded(self) -> None:
        panel = self._organize_panel
        library_id = self._selected_organize_library_id()
        if panel is None or not library_id:
            self._status_text.set("请先在“设置”中创建并启用一个图库。")
            return
        if not self._runtime_controller.is_ready:
            self._status_text.set("后台尚未就绪，智能整理将在启动完成后载入。")
            return
        if panel.library_id != library_id:
            panel.set_library(library_id, refresh=False)
            self._organize_loaded = False
        if self._organize_loaded:
            return
        panel.refresh(
            offset=0,
            prefer_previous=False,
            allow_discard=True,
        )
        self._organize_loaded = True

    def _request_backend_restart(self, reason: str) -> None:
        """Safely reload JSON-backed settings without terminating active jobs."""

        self._assert_ui_thread()
        if self._closed or self._close_pending:
            return
        normalized_reason = reason.strip() or "设置已更新"
        self._restart_generation += 1
        if self._restart_pending:
            self._restart_reason = normalized_reason
            self._status_text.set("设置已保存；正在等待当前安全重启完成。")
            return
        self._restart_pending = True
        self._restart_reason = normalized_reason
        self._restart_wait_notified = False
        self._organize_loaded = False
        self._status_text.set(f"{normalized_reason}；正在安全重启后端…")
        future = self._runtime_controller.stop()
        self._watch_future(future, self._complete_backend_restart)

    def _complete_backend_restart(
        self,
        _value: Any | None,
        error: BaseException | None,
    ) -> None:
        self._assert_ui_thread()
        if self._closed or self._close_pending:
            self._restart_pending = False
            self._cancel_backend_restart_retry()
            return
        if error is not None:
            if (
                isinstance(error, RuntimeOperationError)
                and error.info.category is RuntimeErrorCategory.BUSY
            ):
                message = (
                    "设置已保存，但后台还有任务在运行。为了避免中断导入或标注，"
                    "本次不会强制重启；任务完成后软件会自动应用新设置。"
                )
                self._status_text.set(message)
                if not self._restart_wait_notified:
                    self._restart_wait_notified = True
                    messagebox.showwarning("暂缓重启", message, parent=self.root)
                self._schedule_backend_restart_retry()
                return
            self._restart_pending = False
            message = f"设置已保存，但后端无法安全停止：{error}"
            self._status_text.set(message)
            messagebox.showerror("重启失败", message, parent=self.root)
            return

        self._search_service = None
        self._library_task_service = None
        self._load_library_choices()
        if self._settings_panel is not None:
            self._settings_panel.refresh(reload_json=True)
        self._restart_start_generation = self._restart_generation
        future = self._runtime_controller.start()
        self._watch_future(future, self._finish_backend_restart)

    def _schedule_backend_restart_retry(self) -> None:
        if (
            self._closed
            or self._close_pending
            or not self._restart_pending
            or self._restart_retry_poll is not None
        ):
            return
        self._restart_retry_poll = self.root.after(
            3000,
            self._retry_pending_backend_restart,
        )

    def _cancel_backend_restart_retry(self) -> None:
        if self._restart_retry_poll is None:
            return
        with suppress(tk.TclError):
            self.root.after_cancel(self._restart_retry_poll)
        self._restart_retry_poll = None

    def _retry_pending_backend_restart(self) -> None:
        self._assert_ui_thread()
        self._restart_retry_poll = None
        if self._closed or self._close_pending or not self._restart_pending:
            return
        state = self._runtime_controller.state
        if state in {RuntimeState.STARTING, RuntimeState.STOPPING}:
            self._schedule_backend_restart_retry()
            return
        future = self._runtime_controller.stop()
        self._watch_future(future, self._complete_backend_restart)

    def _finish_backend_restart(
        self,
        _value: Any | None,
        error: BaseException | None,
    ) -> None:
        self._assert_ui_thread()
        if self._closed or self._close_pending:
            self._restart_pending = False
            self._cancel_backend_restart_retry()
            return
        if error is not None:
            self._restart_pending = False
            self._restart_wait_notified = False
            message = f"设置已保存，但后端重新启动失败：{error}"
            self._status_text.set(message)
            self._backend_error_text.set(message)
            messagebox.showerror("重启失败", message, parent=self.root)
            return
        if self._restart_start_generation != self._restart_generation:
            self._restart_wait_notified = False
            self._status_text.set(
                "重启期间检测到新的设置修改；正在再次安全重启以应用最新配置…"
            )
            future = self._runtime_controller.stop()
            self._watch_future(future, self._complete_backend_restart)
            return
        self._restart_pending = False
        self._restart_wait_notified = False
        self._status_text.set(f"{self._restart_reason}；后端已重新启动。")

    def _on_settings_credentials_changed(self, saved: bool) -> None:
        self._assert_ui_thread()
        self._saved_credential_loaded = False
        self._credentials_configured = False
        self._api_key_var.set("")
        if saved and self._runtime_controller.is_ready:
            self._configure_saved_credential_once()
        elif not saved:
            self._backend_error_text.set("已删除保存的 API Key。")

    def _build_search(self, parent: ttk.Frame) -> None:
        card = ttk.Frame(parent, style="Hero.TFrame", padding=(18, 13))
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        card.columnconfigure(0, weight=1)

        heading = ttk.Frame(card, style="Surface.TFrame")
        heading.grid(row=0, column=0, columnspan=2, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="描述你想找的画面", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._search_subtitle_label = ttk.Label(
            heading,
            text="输入人物、角色、作品、动作或神态，也可以附加参考图片",
            style="Hint.TLabel",
        )
        self._search_subtitle_label.grid(row=1, column=0, sticky="w", pady=(3, 0))

        self._backend_error_text = tk.StringVar(value="")
        self._backend_error_label = ttk.Label(
            heading,
            textvariable=self._backend_error_text,
            style="ErrorHint.TLabel",
            justify="right",
        )
        self._backend_error_label.grid(
            row=0, column=1, rowspan=2, sticky="e", padx=(18, 0)
        )

        # Credentials are maintained once in Settings instead of being
        # duplicated across search and task pages.  These variables remain the
        # single session-configuration contract used by backend callbacks.
        self._api_key_var = tk.StringVar(value="")
        self._remember_key_var = tk.BooleanVar(value=False)
        self._configure_key_buttons: list[ttk.Button] = []

        query = ttk.Frame(card, style="Surface.TFrame")
        query.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        query.columnconfigure(1, weight=1)
        self._search_mode_var = tk.StringVar(value=SEMANTIC_MODE_LABEL)
        self._search_mode = ttk.Combobox(
            query,
            textvariable=self._search_mode_var,
            values=SEARCH_MODE_LABELS,
            state="readonly",
            width=10,
        )
        self._search_mode.grid(row=0, column=0, padx=(0, 7))
        self._search_mode.bind("<<ComboboxSelected>>", self._on_search_mode_changed)
        self._query_text_var = tk.StringVar(value="")
        self._query_entry = ttk.Entry(
            query,
            textvariable=self._query_text_var,
            style="Search.TEntry",
        )
        self._query_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self._query_entry.bind("<Return>", lambda _event: self.start_search(), add=True)
        self._choose_query_image_button = ttk.Button(
            query,
            text="添加参考图",
            style="Secondary.TButton",
            command=self.choose_query_image,
        )
        self._choose_query_image_button.grid(row=0, column=2, padx=(0, 8))
        self._search_button = ttk.Button(
            query,
            text="开始搜索",
            style="Primary.TButton",
            command=self.start_search,
            state="disabled",
        )
        self._search_button.grid(row=0, column=3, padx=(0, 8))
        self._cancel_search_button = ttk.Button(
            query,
            text="取消",
            style="Danger.TButton",
            command=self.cancel_search,
            state="disabled",
        )
        self._cancel_search_button.grid(row=0, column=4)

        options = ttk.Frame(card, style="Surface.TFrame")
        options.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        options.columnconfigure(4, weight=1)
        self._all_libraries_var = tk.BooleanVar(value=True)
        self._all_libraries_check = ttk.Checkbutton(
            options,
            text="全部图库",
            variable=self._all_libraries_var,
            command=self._on_all_libraries_changed,
        )
        self._all_libraries_check.grid(row=0, column=0, sticky="nw", padx=(0, 7))
        self._library_list = tk.Listbox(
            options,
            height=2,
            width=28,
            selectmode="extended",
            exportselection=False,
            activestyle="none",
            background=self.theme.surface_muted,
            foreground=self.theme.text,
            selectbackground=self.theme.selection,
            selectforeground=self.theme.text,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=self.theme.border,
        )
        self._library_list.grid(row=0, column=1, sticky="ew", padx=(0, 9))
        self._library_list.bind("<<ListboxSelect>>", self._on_library_selection_changed)
        self._query_image_var = tk.StringVar(value="未选择查询图片")
        self._query_image_label = ttk.Label(
            options,
            textvariable=self._query_image_var,
            style="Hint.TLabel",
        )
        self._query_image_label.grid(row=0, column=2, sticky="nw", padx=(0, 7))
        self._clear_query_image_button = ttk.Button(
            options,
            text="移除",
            style="Quiet.TButton",
            command=self.clear_query_image,
            state="disabled",
        )
        self._clear_query_image_button.grid(row=0, column=3, sticky="n", padx=(0, 12))
        self._search_progress_text = tk.StringVar(value="默认返回 15 张，候选 50 张")
        self._search_progress_label = ttk.Label(
            options,
            textvariable=self._search_progress_text,
            style="Badge.TLabel",
        )
        self._search_progress_label.grid(row=0, column=4, sticky="ne")
        self._search_controls_compact = False

        def sync_backend_error(*_args: object) -> None:
            if self._search_controls_compact and not self._backend_error_text.get():
                self._backend_error_label.grid_remove()
            else:
                self._backend_error_label.grid()

        self._backend_error_text.trace_add("write", sync_backend_error)

        def update_search_controls(event: tk.Event[tk.Misc]) -> None:
            compact = event.width < 900
            if compact == self._search_controls_compact:
                return
            self._search_controls_compact = compact
            if compact:
                self._search_subtitle_label.grid_remove()
                sync_backend_error()
                options.columnconfigure(1, weight=1)
                options.columnconfigure(4, weight=0)
                self._query_entry.grid_configure(
                    row=0, column=1, columnspan=2, sticky="ew", padx=(0, 8)
                )
                self._choose_query_image_button.grid_configure(
                    row=1,
                    column=0,
                    columnspan=2,
                    sticky="w",
                    padx=(0, 8),
                    pady=(7, 0),
                )
                self._search_button.grid_configure(row=0, column=3, padx=0)
                self._cancel_search_button.grid_configure(
                    row=1, column=3, sticky="e", pady=(7, 0)
                )
                self._all_libraries_check.grid_configure(row=0, column=0)
                self._library_list.grid_configure(
                    row=0, column=1, columnspan=2, sticky="ew", padx=0
                )
                self._library_list.configure(height=1)
                self._query_image_label.grid_remove()
                self._search_progress_label.grid_remove()
                self._sync_compact_query_image_controls()
                return

            self._search_subtitle_label.grid()
            sync_backend_error()
            options.columnconfigure(1, weight=0)
            options.columnconfigure(4, weight=1)
            self._query_entry.grid_configure(
                row=0, column=1, columnspan=1, sticky="ew", padx=(0, 8)
            )
            self._choose_query_image_button.grid_configure(
                row=0,
                column=2,
                columnspan=1,
                sticky="",
                padx=(0, 8),
                pady=0,
            )
            self._search_button.grid_configure(row=0, column=3, padx=(0, 8))
            self._cancel_search_button.grid_configure(
                row=0, column=4, sticky="", pady=0
            )
            self._all_libraries_check.grid_configure(row=0, column=0)
            self._library_list.grid_configure(
                row=0, column=1, columnspan=1, sticky="ew", padx=(0, 9)
            )
            self._library_list.configure(height=2)
            self._query_image_label.grid()
            self._query_image_label.grid_configure(
                row=0,
                column=2,
                columnspan=1,
                sticky="nw",
                padx=(0, 7),
                pady=0,
            )
            self._clear_query_image_button.grid_configure(
                row=0, column=3, sticky="n", padx=(0, 12), pady=0
            )
            self._search_progress_label.grid()
            self._search_progress_label.grid_configure(
                row=0, column=4, columnspan=1, sticky="ne", pady=0
            )

        card.bind("<Configure>", update_search_controls, add=True)

    def _build_library_tasks(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        controls = ttk.Frame(parent, style="Hero.TFrame", padding=(18, 15))
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        controls.columnconfigure(0, weight=3)
        controls.columnconfigure(1, weight=2)

        task_heading = ttk.Frame(controls, style="Surface.TFrame")
        task_heading.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        task_heading.columnconfigure(0, weight=1)
        ttk.Label(task_heading, text="批处理工作台", style="Eyebrow.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(task_heading, text="创建图库任务", style="Section.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 0)
        )
        ttk.Label(
            task_heading,
            text="任务在后台并发运行；单张失败会跳过并统一汇总，不会中断整个队列",
            style="Hint.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(3, 0))
        ttk.Label(
            task_heading,
            text="支持并行",
            style="SuccessBadge.TLabel",
        ).grid(row=0, column=1, rowspan=3, sticky="e")

        basics = ttk.Frame(controls, style="Surface.TFrame")
        basics.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        basics.columnconfigure(0, weight=1)
        ttk.Label(basics, text="目标图库", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._task_library_var = tk.StringVar(value="")
        self._task_library_combo = ttk.Combobox(
            basics,
            textvariable=self._task_library_var,
            state="readonly",
        )
        self._task_library_combo.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        ttk.Label(
            basics,
            text="每次任务只处理当前选择的一个图库",
            style="Hint.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Label(
            basics,
            text="本次新增图片标签",
            style="Muted.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=(11, 0))
        self._manual_tags_var = tk.StringVar(value="")
        self._manual_tags_entry = ttk.Entry(
            basics,
            textvariable=self._manual_tags_var,
        )
        self._manual_tags_entry.grid(row=4, column=0, sticky="ew", pady=(5, 0))
        ttk.Label(
            basics,
            text="逗号或换行分隔；不会应用到已有图片",
            style="Hint.TLabel",
        ).grid(row=5, column=0, sticky="w", pady=(4, 0))

        options = ttk.Frame(controls, style="Subtle.TFrame", padding=(13, 11))
        options.grid(row=1, column=1, sticky="nsew")
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="自动标注选项", style="SubtleSection.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 5)
        )
        self._task_recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options,
            text="包含子文件夹",
            variable=self._task_recursive_var,
            style="Subtle.TCheckbutton",
        ).grid(row=1, column=0, sticky="w", padx=(0, 10))
        self._task_verify_hash_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options,
            text="校验文件哈希",
            variable=self._task_verify_hash_var,
            style="Subtle.TCheckbutton",
        ).grid(row=1, column=1, sticky="w")
        self._task_external_confirm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options,
            text="确认图片将发送到阿里云模型",
            variable=self._task_external_confirm_var,
            style="Subtle.TCheckbutton",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Label(options, text="标注模型", style="SubtleMuted.TLabel").grid(
            row=3, column=0, sticky="w", padx=(0, 8), pady=(8, 0)
        )
        self._task_model_var = tk.StringVar(value=DEFAULT_AUTO_TAG_MODEL)
        ttk.Entry(options, textvariable=self._task_model_var).grid(
            row=3, column=1, sticky="ew", pady=(8, 0)
        )
        limits = ttk.Frame(options, style="Subtle.TFrame")
        limits.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        limits.columnconfigure(1, weight=1)
        ttk.Label(limits, text="最多图片", style="SubtleMuted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self._task_max_images_var = tk.StringVar(value=str(DEFAULT_AUTO_TAG_MAX_IMAGES))
        ttk.Spinbox(
            limits,
            from_=1,
            to=10_000,
            textvariable=self._task_max_images_var,
            width=7,
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(limits, text="预算（元）", style="SubtleMuted.TLabel").grid(
            row=0, column=2, sticky="e", padx=(12, 6)
        )
        self._task_budget_var = tk.StringVar(value=str(DEFAULT_AUTO_TAG_BUDGET_CNY))
        ttk.Entry(limits, textvariable=self._task_budget_var, width=8).grid(
            row=0, column=3, sticky="e"
        )

        actions = ttk.Frame(controls, style="Surface.TFrame")
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(13, 0))
        actions.columnconfigure(3, weight=1)
        self._task_action_buttons: list[ttk.Button] = []
        for column, (label, action, primary) in enumerate(
            (
                ("索引并自动标注", "index_and_auto_tag", True),
                ("建立索引", "index", False),
                ("同步图库", "sync", False),
                ("查看统计", "stats", False),
                ("查看根目录", "roots", False),
            )
        ):
            button = ttk.Button(
                actions,
                text=label,
                style="Primary.TButton" if primary else "Quiet.TButton",
                command=self._task_action_command(cast(LibraryTaskAction, action)),
                state="disabled",
            )
            button.grid(
                row=0,
                column=column,
                sticky="e" if column >= 3 else "w",
                padx=(0, 7) if column < 4 else 0,
            )
            self._task_action_buttons.append(button)

        self._task_form_compact = False

        def update_task_form(event: tk.Event[tk.Misc]) -> None:
            compact = event.width < 880
            if compact == self._task_form_compact:
                return
            self._task_form_compact = compact
            if compact:
                basics.grid_configure(row=1, column=0, columnspan=2, padx=0)
                options.grid_configure(
                    row=2,
                    column=0,
                    columnspan=2,
                    pady=(12, 0),
                )
                actions.grid_configure(row=3)
                return
            basics.grid_configure(row=1, column=0, columnspan=1, padx=(0, 14))
            options.grid_configure(
                row=1,
                column=1,
                columnspan=1,
                pady=0,
            )
            actions.grid_configure(row=2)

        controls.bind("<Configure>", update_task_form, add=True)

        content = ttk.Frame(parent, style="App.TFrame")
        content.grid(row=1, column=0, sticky="nsew")
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=5, uniform="task-panes")
        content.columnconfigure(1, weight=3, uniform="task-panes")
        center = ttk.Frame(content, style="Card.TFrame", padding=16)
        details = ttk.Frame(content, style="Card.TFrame", padding=16)
        center.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        details.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        self._task_details_collapsed = False

        def update_task_workspace(event: tk.Event[tk.Misc]) -> None:
            collapsed = event.width < 900
            if collapsed == self._task_details_collapsed:
                return
            self._task_details_collapsed = collapsed
            if collapsed:
                details.grid_remove()
                center.grid_configure(columnspan=2, padx=0)
            else:
                center.grid_configure(columnspan=1, padx=(0, 7))
                details.grid()

        content.bind("<Configure>", update_task_workspace, add=True)

        center.rowconfigure(2, weight=1)
        center.columnconfigure(0, weight=1)
        heading = ttk.Frame(center, style="Surface.TFrame")
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="任务中心", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._task_summary_var = tk.StringVar(value="没有运行中的任务")
        self._task_summary_label = ttk.Label(
            heading,
            textvariable=self._task_summary_var,
            style="Badge.TLabel",
        )
        self._task_summary_label.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._task_refresh_button = ttk.Button(
            heading,
            text="刷新",
            style="Quiet.TButton",
            command=self.refresh_task_center,
            state="disabled",
        )
        self._task_refresh_button.grid(row=0, column=1, rowspan=2)

        self._task_progress = ttk.Progressbar(center, mode="determinate")
        self._task_progress.grid(row=1, column=0, sticky="ew", pady=(9, 8))
        tree_frame = ttk.Frame(center, style="Surface.TFrame")
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self._task_tree = ttk.Treeview(
            tree_frame,
            columns=("action", "library", "status", "progress", "failed", "message"),
            show="headings",
            selectmode="browse",
            height=12,
        )
        columns = {
            "action": ("任务", 130),
            "library": ("图库", 100),
            "status": ("状态", 75),
            "progress": ("进度", 80),
            "failed": ("失败", 55),
            "message": ("信息", 240),
        }
        for name, (label, width) in columns.items():
            self._task_tree.heading(name, text=label)
            self._task_tree.column(
                name,
                width=width,
                minwidth=45,
                stretch=name == "message",
            )
        self._task_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(
            tree_frame,
            orient="vertical",
            command=self._task_tree.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._task_tree.configure(yscrollcommand=scrollbar.set)
        self._task_tree.bind("<<TreeviewSelect>>", self._on_task_selected)
        self._task_tree.tag_configure("active", foreground=self.theme.primary_pressed)
        self._task_tree.tag_configure("success", foreground=self.theme.success)
        self._task_tree.tag_configure("attention", foreground=self.theme.danger)
        self._task_empty_state = ttk.Frame(
            tree_frame,
            style="Surface.TFrame",
            padding=(28, 20),
        )
        ttk.Label(
            self._task_empty_state,
            text="暂无任务",
            style="EmptyTitle.TLabel",
        ).pack()
        ttk.Label(
            self._task_empty_state,
            text="从上方创建任务后，可在这里持续查看进度和错误图片汇总。",
            style="EmptyHint.TLabel",
            justify="center",
        ).pack(pady=(6, 0))
        self._task_empty_state.place(relx=0.5, rely=0.5, anchor="center")

        details.rowconfigure(2, weight=1)
        details.columnconfigure(0, weight=1)
        ttk.Label(details, text="任务详情", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._task_detail_var = tk.StringVar(value="请选择一个任务")
        ttk.Label(
            details,
            textvariable=self._task_detail_var,
            style="Muted.TLabel",
            wraplength=390,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 8))
        result_frame = ttk.Frame(details, style="Surface.TFrame")
        result_frame.grid(row=2, column=0, sticky="nsew")
        result_frame.rowconfigure(0, weight=1)
        result_frame.columnconfigure(0, weight=1)
        self._task_result = tk.Text(
            result_frame,
            wrap="word",
            height=12,
            background=self.theme.code_surface,
            foreground=self.theme.code_text,
            relief="flat",
            padx=8,
            pady=8,
            state="disabled",
        )
        self._task_result.grid(row=0, column=0, sticky="nsew")
        result_scroll = ttk.Scrollbar(
            result_frame,
            orient="vertical",
            command=self._task_result.yview,
        )
        result_scroll.grid(row=0, column=1, sticky="ns")
        self._task_result.configure(yscrollcommand=result_scroll.set)
        detail_actions = ttk.Frame(details, style="Surface.TFrame")
        detail_actions.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        detail_actions.columnconfigure(0, weight=1)
        self._cancel_task_button = ttk.Button(
            detail_actions,
            text="取消所选任务",
            style="Quiet.TButton",
            command=self.cancel_selected_task,
            state="disabled",
        )
        self._cancel_task_button.grid(row=0, column=0, sticky="w")
        self._open_failures_button = ttk.Button(
            detail_actions,
            text="打开错误图片目录",
            style="Quiet.TButton",
            command=self.open_selected_failure_directory,
            state="disabled",
        )
        self._open_failures_button.grid(row=0, column=1, sticky="e")

    def _task_action_command(
        self,
        action: LibraryTaskAction,
    ) -> Callable[[], None]:
        return lambda: self.start_library_task(action)

    def _load_library_choices(self) -> None:
        """Read enabled libraries without changing any collection state."""

        try:
            catalog = ResultCatalog.from_config(self.options.config_path)
        except ResultCatalogError as exc:
            self._libraries = ()
            self._library_list.delete(0, "end")
            self._task_library_combo.configure(values=())
            self._task_library_var.set("")
            self._organize_library_combo.configure(values=())
            self._organize_library_var.set("")
            self._organize_loaded = False
            self._backend_error_text.set(str(exc))
            self._notebook.select(self._settings_page)
            return
        self._libraries = tuple(
            library for library in catalog.libraries if library.enabled
        )
        self._library_list.delete(0, "end")
        for library in self._libraries:
            self._library_list.insert("end", f"{library.name} · {library.library_id}")
        task_values = tuple(
            f"{library.name} · {library.library_id}" for library in self._libraries
        )
        self._task_library_combo.configure(values=task_values)
        self._task_library_var.set(task_values[0] if task_values else "")
        organize_current = (
            self._organize_panel.library_id if self._organize_panel is not None else ""
        )
        self._organize_library_combo.configure(values=task_values)
        organize_index = next(
            (
                index
                for index, library in enumerate(self._libraries)
                if library.library_id == organize_current
            ),
            0,
        )
        if task_values:
            self._organize_library_combo.current(organize_index)
            organize_library_id = self._libraries[organize_index].library_id
            if (
                self._organize_panel is not None
                and self._organize_panel.library_id != organize_library_id
            ):
                self._organize_panel.set_library(organize_library_id, refresh=False)
        else:
            self._organize_library_var.set("")
        self._organize_loaded = False
        self._task_results_directory = catalog.results_directory
        if self._libraries:
            self._library_list.selection_set(0, "end")
            self._library_list.configure(state="disabled")
        else:
            self._all_libraries_var.set(False)
            self._library_list.configure(state="disabled")
            self._notebook.select(self._settings_page)

    def _set_task_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled and self._libraries else "disabled"
        for button in self._task_action_buttons:
            button.configure(state=state)
        self._task_refresh_button.configure(state="normal" if enabled else "disabled")

    def _selected_task_library_id(self) -> str:
        index = self._task_library_combo.current()
        if 0 <= index < len(self._libraries):
            return self._libraries[index].library_id
        return ""

    def _library_task_form_values(self) -> LibraryTaskFormValues:
        return LibraryTaskFormValues(
            library_id=self._selected_task_library_id(),
            manual_tags=self._manual_tags_var.get(),
            recursive=bool(self._task_recursive_var.get()),
            verify_hash=bool(self._task_verify_hash_var.get()),
            model=self._task_model_var.get(),
            max_images=self._task_max_images_var.get(),
            max_budget_cny=self._task_budget_var.get(),
            external_processing_confirmed=bool(self._task_external_confirm_var.get()),
        )

    def start_library_task(self, action: LibraryTaskAction) -> None:
        """Queue one validated library task without disabling other work."""

        service = self._library_task_service
        if service is None or not self._runtime_controller.is_ready:
            self._status_text.set("后台尚未就绪，暂时不能提交图库任务。")
            return
        try:
            request = build_library_task_request(
                action,
                self._library_task_form_values(),
            )
        except (LibraryTaskValidationError, ValueError) as exc:
            self._status_text.set(str(exc))
            messagebox.showwarning("无法提交任务", str(exc), parent=self.root)
            return

        local_id = f"local-{uuid.uuid4().hex}"
        cancel_event = threading.Event()
        self._active_task_cancels[local_id] = cancel_event
        initial_job: dict[str, Any] = {
            "id": local_id,
            "command": request.command,
            "params": dict(request.to_params()),
            "status": "queued",
            "progress": {"message": "正在提交到后台…"},
            "failure_count": 0,
        }
        self._update_task_job(initial_job)
        self._status_text.set("图库任务已进入队列；可以继续提交其他图库任务。")

        def progress(job: dict[str, Any]) -> None:
            self._task_updates.put(copy.deepcopy(job))

        def work() -> _LibraryTaskExecution:
            submission: SubmittedLibraryTask | None = None
            try:
                submission = service.submit(request)
                self._task_updates.put(
                    _OwnedTaskStarted(local_id, submission, cancel_event)
                )
                outcome = service.wait(
                    submission,
                    timeout=24 * 60 * 60.0,
                    poll_interval=0.25,
                    cancel_event=cancel_event,
                    on_progress=progress,
                )
                return _LibraryTaskExecution(local_id, submission, outcome, None)
            except Exception as exc:
                return _LibraryTaskExecution(local_id, submission, None, exc)

        def completed(value: Any | None, error: BaseException | None) -> None:
            if self._closed:
                return
            if error is not None or not isinstance(value, _LibraryTaskExecution):
                failure = error or RuntimeError("图库任务没有返回执行结果。")
                self._finish_failed_library_task(local_id, None, failure)
                return
            submission = value.submission
            job_id = submission.job_id if submission is not None else local_id
            self._active_task_cancels.pop(local_id, None)
            self._active_task_cancels.pop(job_id, None)
            if value.error is not None:
                self._finish_failed_library_task(local_id, submission, value.error)
                return
            if value.outcome is None:
                self._finish_failed_library_task(
                    local_id,
                    submission,
                    RuntimeError("图库任务没有返回完成状态。"),
                )
                return
            self._task_center.remove(local_id)
            self._update_task_job(value.outcome.job)
            view = project_task(value.outcome.job)
            if value.outcome.successful:
                suffix = f"，失败 {view.failed_count} 张" if view.failed_count else ""
                self._status_text.set(f"{view.title}已完成{suffix}。")
            elif value.outcome.status == "cancelled":
                self._status_text.set("任务已取消；已经成功写入的结果会保留。")
            else:
                self._status_text.set(f"{view.title}未成功，请在任务中心查看错误。")

        self._submit_background(work, completed)

    def _finish_failed_library_task(
        self,
        local_id: str,
        submission: SubmittedLibraryTask | None,
        error: BaseException,
    ) -> None:
        job_id = submission.job_id if submission is not None else local_id
        self._task_center.remove(local_id)
        existing = self._task_center.get(job_id) or {
            "id": job_id,
            "command": submission.command if submission is not None else "library_task",
            "params": (dict(submission.params) if submission is not None else {}),
        }
        existing.update(
            {
                "status": "failed",
                "progress": {"message": "任务失败。"},
                "error": {
                    "code": "desktop_task_failed",
                    "message": str(error) or type(error).__name__,
                    "details": {"type": type(error).__name__},
                },
            }
        )
        self._active_task_cancels.pop(local_id, None)
        self._active_task_cancels.pop(job_id, None)
        self._update_task_job(existing)
        self._status_text.set(f"图库任务失败：{error} 软件仍可继续使用。")

    def _handle_owned_task_started(self, event: _OwnedTaskStarted) -> None:
        self._task_center.remove(event.local_id)
        self._active_task_cancels.pop(event.local_id, None)
        self._active_task_cancels[event.submission.job_id] = event.cancel_event
        job = copy.deepcopy(event.submission.submitted_job)
        job.setdefault("command", event.submission.command)
        job.setdefault("params", dict(event.submission.params))
        job.setdefault("status", "queued")
        self._update_task_job(job)

    def _update_task_job(
        self,
        job: dict[str, Any],
        *,
        refresh: bool = True,
    ) -> bool:
        try:
            changed = self._task_center.update(job)
        except (TypeError, ValueError):
            return False
        if changed and refresh:
            self._refresh_task_tree()
        return changed

    def _refresh_task_tree(self) -> None:
        self._assert_ui_thread()
        selected = self._selected_task_id()
        for item in self._task_tree.get_children(""):
            self._task_tree.delete(item)
        jobs = self._task_center.jobs
        if jobs:
            self._task_empty_state.place_forget()
        else:
            self._task_empty_state.place(relx=0.5, rely=0.5, anchor="center")
        active = 0
        attention = 0
        for job in jobs:
            view = project_task(job)
            active += int(view.active)
            attention += int(
                view.failed_count > 0 or view.status in {"failed", "needs_attention"}
            )
            row_tone = (
                "active"
                if view.active
                else "attention"
                if view.failed_count > 0
                or view.status in {"failed", "needs_attention", "partial"}
                else "success"
                if view.status == "succeeded"
                else ""
            )
            self._task_tree.insert(
                "",
                "end",
                iid=view.job_id,
                tags=(row_tone,) if row_tone else (),
                values=(
                    view.title,
                    view.library,
                    view.status_text,
                    view.progress_text,
                    view.failed_count or "",
                    view.message,
                ),
            )
        if selected and self._task_tree.exists(selected):
            self._task_tree.selection_set(selected)
        elif jobs:
            self._task_tree.selection_set(str(jobs[0]["id"]))
        self._task_summary_var.set(
            f"运行中 {active} 个任务"
            + (f" · 需查看 {attention} 个" if attention else "")
            if active
            else f"已完成 · 需查看 {attention} 个任务"
            if attention
            else "没有运行中的任务"
        )
        self._on_task_selected(None)

    def _selected_task_id(self) -> str | None:
        selection = self._task_tree.selection()
        return selection[0] if selection else None

    def _on_task_selected(self, _event: tk.Event[Any] | None) -> None:
        job_id = self._selected_task_id()
        job = self._task_center.get(job_id) if job_id else None
        if job is None:
            self._task_detail_var.set("请选择一个任务")
            self._set_task_result_text("")
            self._cancel_task_button.configure(state="disabled")
            self._open_failures_button.configure(state="disabled")
            self._task_progress.stop()
            self._task_progress.configure(mode="determinate", value=0, maximum=1)
            return
        view = project_task(job)
        self._task_detail_var.set(
            " · ".join(
                value
                for value in (
                    view.title,
                    view.library,
                    view.status_text,
                    view.message,
                )
                if value
            )
        )
        self._set_task_result_text(task_result_text(job))
        self._cancel_task_button.configure(
            state="normal" if view.cancellable else "disabled"
        )
        failure_directory = self._failure_directory_for_job(job)
        self._open_failures_button.configure(
            state="normal" if failure_directory is not None else "disabled"
        )
        progress = job.get("progress")
        current = progress.get("current") if isinstance(progress, dict) else None
        total = progress.get("total") if isinstance(progress, dict) else None
        self._task_progress.stop()
        if (
            isinstance(current, int)
            and not isinstance(current, bool)
            and isinstance(total, int)
            and not isinstance(total, bool)
            and total > 0
        ):
            self._task_progress.configure(
                mode="determinate",
                maximum=total,
                value=min(max(0, current), total),
            )
        elif view.active:
            self._task_progress.configure(mode="indeterminate", maximum=100, value=0)
            self._task_progress.start(50)
        else:
            self._task_progress.configure(mode="determinate", maximum=1, value=1)

    def _set_task_result_text(self, value: str) -> None:
        self._task_result.configure(state="normal")
        self._task_result.delete("1.0", "end")
        if value:
            self._task_result.insert("1.0", value)
        self._task_result.configure(state="disabled")

    def _failure_directory_for_job(self, job: dict[str, Any]) -> Path | None:
        if self._task_results_directory is None:
            return None
        return resolve_failure_directory(self._task_results_directory, job)

    def cancel_selected_task(self) -> None:
        job_id = self._selected_task_id()
        if not job_id:
            return
        job = self._task_center.get(job_id)
        if job is None or task_is_terminal(job):
            return
        cancel_event = self._active_task_cancels.get(job_id)
        if cancel_event is not None:
            cancel_event.set()
            job["status"] = "cancelling"
            job["progress"] = {"message": "正在等待当前步骤结束并取消…"}
            self._update_task_job(job)
        else:
            self._runtime_controller.cancel_job(job_id)
        self._status_text.set("取消请求已发送；其他任务会继续运行。")

    def open_selected_failure_directory(self) -> None:
        job_id = self._selected_task_id()
        job = self._task_center.get(job_id) if job_id else None
        if job is None:
            return
        directory = self._failure_directory_for_job(job)
        if directory is None:
            self._status_text.set("错误图片目录不可用或未通过安全校验。")
            return
        self._open_path(directory)

    def refresh_task_center(self) -> None:
        if not self._runtime_controller.is_ready:
            return
        future = self._task_refresh_future
        if future is not None and not future.done():
            return
        self._task_refresh_future = self._runtime_controller.list_jobs(limit=50)
        self._next_task_refresh_at = time.monotonic() + 2.0

    def _maybe_refresh_task_center(self) -> None:
        if (
            self._runtime_controller.is_ready
            and time.monotonic() >= self._next_task_refresh_at
        ):
            self.refresh_task_center()

    def _on_all_libraries_changed(self) -> None:
        self._assert_ui_thread()
        if not self._libraries:
            self._all_libraries_var.set(False)
            return
        if self._all_libraries_var.get():
            self._library_list.configure(state="normal")
            self._library_list.selection_set(0, "end")
            self._library_list.configure(state="disabled")
        else:
            self._library_list.configure(state="normal")

    def _on_library_selection_changed(self, _event: tk.Event[Any]) -> None:
        self._assert_ui_thread()
        if not self._all_libraries_var.get():
            selected = len(self._library_list.curselection())
            self._search_progress_text.set(
                f"已选 {selected} 个图库 · 默认返回 15 张，候选 50 张"
            )

    def _selected_library_ids(self) -> tuple[str, ...]:
        return tuple(
            self._libraries[index].library_id
            for index in self._library_list.curselection()
            if 0 <= index < len(self._libraries)
        )

    def _on_search_mode_changed(self, _event: tk.Event[Any]) -> None:
        self._assert_ui_thread()
        if self._search_mode_var.get() == TAG_MODE_LABEL:
            self.clear_query_image()
            self._choose_query_image_button.configure(state="disabled")
            self._search_progress_text.set("标签支持模糊包含，例如“原”可命中“原神”")
        else:
            self._choose_query_image_button.configure(
                state="disabled" if self._search_running else "normal"
            )
            self._search_progress_text.set("默认返回 15 张，候选 50 张")

    def choose_query_image(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择用于搜索的图片",
            filetypes=(
                ("图片", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tif *.tiff"),
                ("所有文件", "*.*"),
            ),
        )
        if not selected:
            return
        self._query_image_path = Path(selected).expanduser()
        self._query_image_var.set(self._query_image_path.name)
        self._clear_query_image_button.configure(state="normal")
        self._sync_compact_query_image_controls()

    def clear_query_image(self) -> None:
        self._query_image_path = None
        self._query_image_var.set("未选择查询图片")
        self._clear_query_image_button.configure(state="disabled")
        self._sync_compact_query_image_controls()

    def _sync_compact_query_image_controls(self) -> None:
        """Keep compact image-query feedback useful without adding a row."""

        if not self._search_controls_compact:
            self._choose_query_image_button.configure(text="参考图片")
            self._clear_query_image_button.grid()
            return
        if self._query_image_path is None:
            self._choose_query_image_button.configure(text="参考图片")
            self._clear_query_image_button.grid_remove()
            return
        name = self._query_image_path.name
        compact_name = name if len(name) <= 12 else f"{name[:11]}…"
        self._choose_query_image_button.configure(text=f"图片 · {compact_name}")
        self._clear_query_image_button.grid()
        self._clear_query_image_button.grid_configure(
            row=0, column=3, sticky="e", padx=0, pady=0
        )

    def start_backend(self) -> None:
        """Start the native Python backend without blocking Tk's event loop."""

        if self._closed or self._close_pending:
            return
        state = self._runtime_controller.state
        if state in {RuntimeState.STARTING, RuntimeState.STOPPING}:
            return
        self._backend_error_text.set("")
        self._set_backend_status("正在启动…", "starting")
        self._retry_backend_button.configure(state="disabled")
        # Runtime events, including errors, are consumed by the main-thread
        # polling loop.  No worker callback accesses tkinter state.
        self._runtime_controller.start()

    def _handle_runtime_event(self, event: RuntimeEvent) -> None:
        self._assert_ui_thread()
        if event.kind is RuntimeEventKind.STATE_CHANGED:
            if event.state is RuntimeState.STARTING:
                self._credentials_configured = False
                self._saved_credential_loaded = False
                self._organize_loaded = False
                self._search_service = None
                self._library_task_service = None
            state_text, tone = {
                RuntimeState.STOPPED: ("已停止", "muted"),
                RuntimeState.STARTING: ("正在启动…", "starting"),
                RuntimeState.READY: ("已就绪", "success"),
                RuntimeState.STOPPING: ("正在停止…", "starting"),
                RuntimeState.ERROR: ("不可用", "error"),
                RuntimeState.DISPOSED: ("已关闭", "muted"),
            }[event.state]
            self._set_backend_status(state_text, tone)
            self._retry_backend_button.configure(
                state=(
                    "normal"
                    if event.state in {RuntimeState.STOPPED, RuntimeState.ERROR}
                    else "disabled"
                )
            )
            if event.state is not RuntimeState.READY:
                self._search_button.configure(state="disabled")
                self._set_configure_key_buttons_state("disabled")
                self._set_task_actions_enabled(False)
        elif event.kind is RuntimeEventKind.RUNTIME_READY:
            runtime = event.runtime
            if runtime is None:
                self._backend_error_text.set("后端已启动，但没有连接信息。")
                return
            try:
                self._search_service = self._search_service_factory(runtime)
                self._library_task_service = self._library_task_service_factory(runtime)
            except Exception as exc:
                self._search_service = None
                self._library_task_service = None
                self._backend_error_text.set(f"无法初始化桌面服务：{exc}")
                return
            self._set_backend_status("已就绪", "success")
            self._backend_error_text.set("")
            self._search_button.configure(state="normal")
            self._set_configure_key_buttons_state("normal")
            self._set_task_actions_enabled(True)
            self.refresh_task_center()
            self._configure_saved_credential_once()
            if self._notebook.select() == str(self._organize_page):
                self._ensure_organize_loaded()
        elif event.kind is RuntimeEventKind.JOB_UPDATED:
            if event.job is not None:
                self._update_task_job(event.job)
        elif event.kind is RuntimeEventKind.JOBS_LISTED:
            changed = False
            for job in event.jobs:
                changed |= self._update_task_job(job, refresh=False)
            if changed:
                self._refresh_task_tree()
            self._task_refresh_future = None
        elif event.kind in {RuntimeEventKind.ERROR, RuntimeEventKind.WARNING}:
            if event.error is None:
                return
            prefix = "警告" if event.kind is RuntimeEventKind.WARNING else "错误"
            self._backend_error_text.set(f"{prefix}：{event.error.message}")
            if event.kind is RuntimeEventKind.ERROR:
                self._set_backend_status("不可用", "error")
            if event.operation == "list_jobs":
                self._task_refresh_future = None

    def _set_backend_status(self, text: str, tone: str) -> None:
        """Update backend wording and colour as one semantic operation."""

        styles = {
            "muted": "StatusMuted.TLabel",
            "starting": "StatusStarting.TLabel",
            "success": "StatusSuccess.TLabel",
            "error": "StatusError.TLabel",
        }
        try:
            style = styles[tone]
        except KeyError as exc:
            raise ValueError(f"Unsupported backend status tone: {tone}") from exc
        self._backend_status_text.set(text)
        self._backend_status_label.configure(style=style)
        self._backend_status_dot.configure(style=style)
        if tone == "error" or text in {"已停止", "不可用"}:
            self._retry_backend_button.grid()
        else:
            self._retry_backend_button.grid_remove()

    def _configure_saved_credential_once(self) -> None:
        if self._saved_credential_loaded:
            return
        self._saved_credential_loaded = True
        try:
            secret = self._credential_store.read_secret()
        except Exception as exc:
            self._backend_error_text.set(f"无法读取已保存密钥：{exc}")
            return
        if secret:
            self._configure_api_key_value(secret, remember=False, saved=True)

    def configure_api_key(self) -> None:
        api_key = self._api_key_var.get().strip()
        if not api_key:
            messagebox.showwarning(
                "缺少 API Key",
                "请前往“设置 → 模型与密钥”保存阿里云百炼 API Key。",
                parent=self.root,
            )
            return
        self._configure_api_key_value(
            api_key,
            remember=bool(self._remember_key_var.get()),
            saved=False,
        )

    def _set_configure_key_buttons_state(self, state: str) -> None:
        for button in self._configure_key_buttons:
            button.configure(state=state)

    def _configure_api_key_value(
        self,
        api_key: str,
        *,
        remember: bool,
        saved: bool,
    ) -> None:
        if not self._runtime_controller.is_ready:
            self._backend_error_text.set("后台尚未就绪，暂时无法应用密钥。")
            return
        self._set_configure_key_buttons_state("disabled")
        self._set_backend_status("正在应用密钥…", "starting")
        future = self._runtime_controller.configure_credentials(api_key)

        def completed(_value: Any | None, error: BaseException | None) -> None:
            if self._closed:
                return
            self._set_configure_key_buttons_state("normal")
            if error is not None:
                self._credentials_configured = False
                self._set_backend_status("已就绪", "success")
                self._backend_error_text.set(f"密钥不可用：{error}")
                return
            self._credentials_configured = True
            self._set_backend_status("已就绪 · 密钥已配置", "success")
            self._backend_error_text.set("")
            if not saved:
                self._api_key_var.set("")
            if remember and self._credential_store.persistent:
                try:
                    self._credential_store.save_secret(api_key)
                except Exception as exc:
                    self._backend_error_text.set(f"密钥已应用，但安全保存失败：{exc}")

        self._watch_future(future, completed)

    def _search_request_from_controls(self) -> SearchRequest:
        return build_search_request(
            SearchFormValues(
                query_text=self._query_text_var.get(),
                query_image=(
                    str(self._query_image_path)
                    if self._query_image_path is not None
                    else ""
                ),
                mode_label=self._search_mode_var.get(),
                all_libraries=bool(self._all_libraries_var.get()),
                selected_library_ids=self._selected_library_ids(),
                top_k=15,
                candidate_k=50,
            ),
            enabled_library_ids=tuple(
                library.library_id for library in self._libraries
            ),
        )

    def start_search(self) -> None:
        """Submit and monitor one search entirely outside Tk's main thread."""

        service = self._search_service
        if service is None or not self._runtime_controller.is_ready:
            self._status_text.set("后台尚未就绪，请稍候或点击“重试”。")
            return
        if self._search_running:
            return
        try:
            request = self._search_request_from_controls()
        except (SearchValidationError, OSError, ValueError) as exc:
            self._status_text.set(str(exc))
            messagebox.showwarning("无法开始搜索", str(exc), parent=self.root)
            return

        self._search_cancel = threading.Event()
        cancel_event = self._search_cancel
        generation = self._search_generation = self._search_generation + 1
        self._set_search_running(True)
        self._search_progress_text.set("正在提交搜索…")
        self._status_text.set("正在搜索，界面可以继续浏览现有结果。")

        def progress(job: dict[str, Any]) -> None:
            # This callback runs on a worker.  Only immutable data crosses into
            # the queue; the Tk polling loop performs all visible updates.
            self._search_progress.put(copy.deepcopy(job))

        def work() -> SearchOutcome:
            submission = service.submit(request)
            return service.wait(
                submission,
                timeout=3600.0,
                poll_interval=0.25,
                cancel_event=cancel_event,
                on_progress=progress,
            )

        def completed(value: Any | None, error: BaseException | None) -> None:
            if self._closed or generation != self._search_generation:
                return
            self._set_search_running(False)
            if error is not None:
                message = f"搜索失败：{error} 原有结果保持不变。"
                self._status_text.set(message)
                self._search_progress_text.set("搜索失败")
                messagebox.showerror("搜索失败", message, parent=self.root)
                return
            if not isinstance(value, SearchOutcome):
                message = "搜索任务没有返回有效结果，原有结果保持不变。"
                self._status_text.set(message)
                messagebox.showerror("搜索失败", message, parent=self.root)
                return
            if not value.successful or value.manifest_path is None:
                message = outcome_error_text(value.status, value.error)
                self._status_text.set(message)
                self._search_progress_text.set(message)
                if value.status != "cancelled":
                    messagebox.showerror("搜索未完成", message, parent=self.root)
                return
            self._search_progress_text.set("搜索完成，正在载入结果…")
            self._load_search_manifest(value.manifest_path)

        self._submit_background(work, completed)

    def cancel_search(self) -> None:
        if not self._search_running:
            return
        self._search_cancel.set()
        self._cancel_search_button.configure(state="disabled")
        self._search_progress_text.set("正在请求取消，等待后台确认…")

    def _set_search_running(self, running: bool) -> None:
        self._search_running = running
        self._search_button.configure(
            state=(
                "disabled"
                if running or not self._runtime_controller.is_ready
                else "normal"
            )
        )
        self._cancel_search_button.configure(state="normal" if running else "disabled")
        entry_state = "disabled" if running else "normal"
        self._query_entry.configure(state=entry_state)
        self._search_mode.configure(state="disabled" if running else "readonly")
        self._all_libraries_check.configure(state=entry_state)
        if running:
            self._library_list.configure(state="disabled")
            self._choose_query_image_button.configure(state="disabled")
            self._clear_query_image_button.configure(state="disabled")
        else:
            self._on_all_libraries_changed()
            mode_is_tags = self._search_mode_var.get() == TAG_MODE_LABEL
            self._choose_query_image_button.configure(
                state="disabled" if mode_is_tags else "normal"
            )
            self._clear_query_image_button.configure(
                state="normal" if self._query_image_path is not None else "disabled"
            )

    def _handle_search_progress(self, job: dict[str, Any]) -> None:
        self._assert_ui_thread()
        message = format_search_progress(job)
        self._search_progress_text.set(message)
        self._status_text.set(message)

    def _load_search_manifest(self, manifest_path: Path) -> None:
        """Atomically replace the gallery only after a new manifest loads."""

        self._load_cancel.set()
        cancel = self._load_cancel = threading.Event()
        generation = self._load_generation = self._load_generation + 1
        self._set_busy(True, "正在读取新的搜索结果…")

        def work() -> tuple[ResultCatalog, SearchResultPage]:
            if cancel.is_set():
                raise CancelledError()
            catalog = ResultCatalog.from_config(self.options.config_path)
            page = catalog.load_manifest(
                manifest_path,
                page=1,
                page_size=self.options.page_size,
            )
            if cancel.is_set():
                raise CancelledError()
            return catalog, page

        def completed(value: Any | None, error: BaseException | None) -> None:
            if self._closed or generation != self._load_generation:
                return
            self._set_busy(False)
            if error is not None:
                if isinstance(error, CancelledError):
                    return
                message = f"搜索已完成，但结果无法显示：{error} 原有结果保持不变。"
                self._status_text.set(message)
                self._search_progress_text.set("结果载入失败")
                messagebox.showerror("无法显示搜索结果", message, parent=self.root)
                return
            if value is None:
                self._status_text.set("搜索结果为空，原有结果保持不变。")
                return
            catalog, page = value
            self._catalog = catalog
            self._source_kind = "manifest"
            self._source_path = manifest_path
            self._show_page(page)
            self._search_progress_text.set("搜索结果已载入")

        self._submit_background(work, completed)

    def _build_gallery(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)
        heading = ttk.Frame(parent, style="Surface.TFrame")
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="结果画廊", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._summary_text = tk.StringVar(value="尚未载入")
        ttk.Label(heading, textvariable=self._summary_text, style="Badge.TLabel").grid(
            row=0, column=1, sticky="e"
        )

        self._gallery: ResponsiveGallery[SearchResult] = ResponsiveGallery(
            parent,
            self._image_dispatcher,
            theme=self.theme,
            max_columns=5,
        )
        self._gallery.grid(row=1, column=0, sticky="nsew")

        pager = ttk.Frame(parent, style="Surface.TFrame")
        pager.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        pager.columnconfigure(1, weight=1)
        self._previous_button = ttk.Button(
            pager,
            text="上一页",
            style="Quiet.TButton",
            command=self.previous_page,
            state="disabled",
        )
        self._previous_button.grid(row=0, column=0)
        self._page_text = tk.StringVar(value="第 0 / 0 页")
        ttk.Label(pager, textvariable=self._page_text, style="Muted.TLabel").grid(
            row=0, column=1
        )
        self._next_button = ttk.Button(
            pager,
            text="下一页",
            style="Quiet.TButton",
            command=self.next_page,
            state="disabled",
        )
        self._next_button.grid(row=0, column=2)

    def _build_preview(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)
        preview_heading = ttk.Frame(parent, style="Surface.TFrame")
        preview_heading.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        preview_heading.columnconfigure(0, weight=1)
        ttk.Label(preview_heading, text="图片详情", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            preview_heading,
            text="完整显示 · 不裁剪",
            style="Hint.TLabel",
        ).grid(row=0, column=1, sticky="e")

        preview_surface = tk.Frame(
            parent,
            background=self.theme.preview,
            highlightthickness=1,
            highlightbackground=self.theme.border_strong,
            padx=5,
            pady=5,
        )
        preview_surface.grid(row=1, column=0, sticky="nsew")
        preview_surface.rowconfigure(0, weight=1)
        preview_surface.columnconfigure(0, weight=1)
        self._preview = AsyncImageCanvas(
            preview_surface,
            self._image_dispatcher,
            mode="contain",
            background=self.theme.preview,
            placeholder="请选择一张图片",
        )
        self._preview.grid(row=0, column=0, sticky="nsew")

        details = ttk.Frame(parent, style="Surface.TFrame")
        details.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        details.columnconfigure(0, weight=1)
        self._preview_title = tk.StringVar(value="请选择结果图片")
        self._preview_title_label = ttk.Label(
            details,
            textvariable=self._preview_title,
            style="PreviewTitle.TLabel",
            wraplength=360,
        )
        self._preview_title_label.grid(row=0, column=0, sticky="w")
        self._preview_meta = tk.StringVar(value="—")
        self._preview_meta_label = ttk.Label(
            details,
            textvariable=self._preview_meta,
            style="Hint.TLabel",
            wraplength=360,
            justify="left",
        )
        self._preview_meta_label.grid(row=1, column=0, sticky="w", pady=(5, 0))
        self._preview_tags = tk.StringVar(value="标签：—")
        self._preview_tags_label = ttk.Label(
            details,
            textvariable=self._preview_tags,
            style="Hint.TLabel",
            wraplength=360,
            justify="left",
        )
        self._preview_tags_label.grid(row=2, column=0, sticky="w", pady=(4, 0))

        def update_preview_wrap(event: tk.Event[tk.Misc]) -> None:
            wraplength = max(140, event.width - 4)
            for label in (
                self._preview_title_label,
                self._preview_meta_label,
                self._preview_tags_label,
            ):
                label.configure(wraplength=wraplength)

        details.bind("<Configure>", update_preview_wrap, add=True)
        buttons = ttk.Frame(details, style="Surface.TFrame")
        buttons.grid(row=3, column=0, sticky="ew", pady=(11, 0))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        self._open_button = ttk.Button(
            buttons,
            text="系统打开",
            style="Primary.TButton",
            command=self.open_selected,
            state="disabled",
        )
        self._open_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self._folder_open_button = ttk.Button(
            buttons,
            text="所在文件夹",
            style="Quiet.TButton",
            command=self.open_selected_folder,
            state="disabled",
        )
        self._folder_open_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))

    def _load_initial_source(self) -> None:
        if self.options.manifest_path is not None:
            self._source_kind = "manifest"
            self._source_path = self.options.manifest_path
        elif self.options.image_folder is not None:
            self._source_kind = "folder"
            self._source_path = self.options.image_folder
        else:
            self._source_kind = "latest"
        self._load_page(1)

    def load_latest(self) -> None:
        self._catalog = None
        self._source_kind = "latest"
        self._source_path = None
        self._load_page(1)

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择要浏览的图片文件夹",
            mustexist=True,
        )
        if not selected:
            return
        self._catalog = None
        self._source_kind = "folder"
        self._source_path = Path(selected)
        self._load_page(1)

    def previous_page(self) -> None:
        if self._page is not None and self._page.has_previous:
            self._load_page(self._page.page - 1)

    def next_page(self) -> None:
        if self._page is not None and self._page.has_next:
            self._load_page(self._page.page + 1)

    def _load_page(self, page_number: int) -> None:
        self._load_cancel.set()
        cancel = self._load_cancel = threading.Event()
        generation = self._load_generation = self._load_generation + 1
        source_kind = self._source_kind
        source_path = self._source_path
        cached_catalog = self._catalog
        self._set_busy(True, "正在读取图片和结果清单…")

        def work() -> tuple[ResultCatalog, SearchResultPage]:
            if cancel.is_set():
                raise CancelledError()
            catalog = cached_catalog or self._catalog_for_source(
                source_kind, source_path
            )
            if source_kind == "folder":
                assert source_path is not None
                page = catalog.from_folder(
                    source_path,
                    page=page_number,
                    page_size=self.options.page_size,
                    cancelled=cancel.is_set,
                )
            elif source_kind == "manifest":
                assert source_path is not None
                page = catalog.load_manifest(
                    source_path,
                    page=page_number,
                    page_size=self.options.page_size,
                )
            else:
                page = catalog.load_latest(
                    page=page_number,
                    page_size=self.options.page_size,
                )
            if cancel.is_set():
                raise CancelledError()
            return catalog, page

        def completed(value: Any | None, error: BaseException | None) -> None:
            if self._closed or generation != self._load_generation:
                return
            self._set_busy(False)
            if error is not None:
                if isinstance(error, CancelledError):
                    return
                self._show_load_error(error)
                return
            if value is None:
                self._show_load_error(RuntimeError("图片读取任务没有返回结果。"))
                return
            catalog, page = value
            self._catalog = catalog
            self._show_page(page)

        self._submit_background(work, completed)

    def _catalog_for_source(
        self, source_kind: str, source_path: Path | None
    ) -> ResultCatalog:
        try:
            return ResultCatalog.from_config(self.options.config_path)
        except ResultCatalogError:
            if source_kind == "folder" and source_path is not None:
                return self._temporary_catalog(source_path)
            if source_kind == "manifest" and source_path is not None:
                return self._temporary_catalog(source_path.parent.parent)
            raise

    @staticmethod
    def _temporary_catalog(folder: Path) -> ResultCatalog:
        root = folder.expanduser().resolve()
        library = LibraryRecord("folder", root.name or "图片文件夹", root, True)
        return ResultCatalog(
            CatalogConfig(
                config_path=root / ".zvec-desktop-folder.json",
                results_directory=root,
                default_library_id=library.library_id,
                libraries=(library,),
            )
        )

    def _show_page(self, page: SearchResultPage) -> None:
        self._page = page
        self._items = page.items
        self._selected_index = None
        self._summary_text.set(page.summary)
        self._source_text.set(page.source_label)
        if self._notebook.select() == str(self._main_pages[0]):
            self._page_subtitle_text.set(page.source_label)
        current = page.page if page.total_pages else 0
        self._page_text.set(f"第 {current} / {page.total_pages} 页")
        self._previous_button.configure(
            state="normal" if page.has_previous else "disabled"
        )
        self._next_button.configure(state="normal" if page.has_next else "disabled")
        self._gallery.set_items(
            page.items,
            on_select=self._select_result,
        )
        if page.items:
            self._gallery.select(0)
            self._status_text.set(f"已载入 {len(page.items)} 张图片")
        else:
            self._clear_preview()
            self._status_text.set("当前页面没有可显示图片")

    def _select_result(self, index: int, item: SearchResult) -> None:
        self._selected_index = index
        self._preview.set_image(item.display_path)
        self._preview_title.set(item.name)
        confidence = f"置信度 {item.confidence:.3f}" if item.confidence else ""
        source = {
            "image": "图片匹配",
            "text": "文字匹配",
            "metadata": "描述匹配",
            "fused": "联合匹配",
            "tag": "标签匹配",
            "folder": "文件夹图片",
        }.get(item.rank_source, item.rank_source)
        self._preview_meta.set(
            " · ".join(
                value
                for value in (
                    item.library_name,
                    source,
                    confidence,
                    item.relative_path,
                )
                if value
            )
        )
        tags = item.matched_tags or item.tags
        prefix = "命中标签" if item.matched_tags else "标签"
        self._preview_tags.set(f"{prefix}：{'、'.join(tags)}" if tags else "标签：—")
        for button in (self._open_button, self._folder_open_button):
            button.configure(state="normal")

    def _clear_preview(self) -> None:
        self._selected_index = None
        self._preview.set_image(None)
        self._preview_title.set("请选择结果图片")
        self._preview_meta.set("—")
        self._preview_tags.set("标签：—")
        for button in (self._open_button, self._folder_open_button):
            button.configure(state="disabled")

    def open_selected(self) -> None:
        item = self._selected_item()
        if item is not None:
            self._open_path(item.display_path)

    def open_selected_folder(self) -> None:
        item = self._selected_item()
        if item is not None:
            self._open_path(item.display_path.parent)

    def _selected_item(self) -> SearchResult | None:
        if self._selected_index is None or self._selected_index >= len(self._items):
            return None
        return self._items[self._selected_index]

    def _load_fitted_image(
        self, path: Path, size: tuple[int, int], mode: str
    ) -> Image.Image:
        result: ImageLoadResult = self._image_loader.load(path, size, mode)
        if result.image is None:
            message = result.error.message if result.error is not None else "图片不可用"
            raise RuntimeError(message)
        return result.image

    def _submit_background(
        self,
        operation: Callable[[], Any],
        completion: BackgroundCompletion,
    ) -> None:
        with self._state_lock:
            if self._closed:
                return
            try:
                future = self._background.submit(operation)
            except RuntimeError:
                return
            completion_id = self._next_completion_id
            self._next_completion_id += 1
            self._pending_completions[completion_id] = completion
        self._attach_future(future, completion_id)

    def _watch_future(
        self,
        future: Future[Any],
        completion: BackgroundCompletion,
    ) -> None:
        """Deliver a foreign future to Tk through the existing main-thread queue."""

        if future.done():
            try:
                value = future.result()
            except Exception as exc:
                completion(None, exc)
            else:
                completion(value, None)
            return
        with self._state_lock:
            if self._closed:
                return
            completion_id = self._next_completion_id
            self._next_completion_id += 1
            self._pending_completions[completion_id] = completion
        self._attach_future(future, completion_id)

    def _attach_future(self, future: Future[Any], completion_id: int) -> None:
        window_ref = weakref.ref(self)

        def collect(completed: Future[Any]) -> None:
            window = window_ref()
            if window is not None:
                window._collect_background(completed, completion_id)

        future.add_done_callback(collect)

    def _collect_background(
        self,
        future: Future[Any],
        completion_id: int,
    ) -> None:
        try:
            value = future.result()
        except Exception as exc:
            with self._state_lock:
                if not self._closed:
                    self._background_results.put((completion_id, None, exc))
        else:
            with self._state_lock:
                if not self._closed:
                    self._background_results.put((completion_id, value, None))

    def _drain_background(self) -> None:
        self._assert_ui_thread()
        with self._state_lock:
            if self._closed:
                return
        self._runtime_controller.drain_events(self._handle_runtime_event)
        for _ in range(64):
            try:
                task_update = self._task_updates.get_nowait()
            except queue.Empty:
                break
            if isinstance(task_update, _OwnedTaskStarted):
                self._handle_owned_task_started(task_update)
            else:
                self._update_task_job(task_update)
        for _ in range(32):
            try:
                progress = self._search_progress.get_nowait()
            except queue.Empty:
                break
            self._handle_search_progress(progress)
        for _ in range(32):
            try:
                completion_id, value, error = self._background_results.get_nowait()
            except queue.Empty:
                break
            completion = self._pending_completions.pop(completion_id, None)
            if completion is None:
                continue
            completion(value, error)
        self._maybe_refresh_task_center()
        with self._state_lock:
            if self._closed:
                return
            with suppress(tk.TclError):
                self._background_poll = self.root.after(35, self._drain_background)

    def _assert_ui_thread(self) -> None:
        if threading.get_ident() != self._ui_thread_id:
            raise RuntimeError("tkinter updates must run on the desktop main thread")

    def _set_busy(self, busy: bool, message: str | None = None) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self._latest_button.configure(state=state)
        self._folder_button.configure(state=state)
        if message:
            self._status_text.set(message)

    def _show_load_error(self, error: BaseException) -> None:
        self._catalog = None
        self._page = None
        self._items = ()
        self._clear_preview()
        self._gallery.set_items((), on_select=self._select_result)
        self._summary_text.set("没有可显示的图片")
        self._source_text.set("请选择图片文件夹")
        if self._notebook.select() == str(self._main_pages[0]):
            self._page_subtitle_text.set("请选择图片文件夹")
        self._page_text.set("第 0 / 0 页")
        self._previous_button.configure(state="disabled")
        self._next_button.configure(state="disabled")
        self._status_text.set(str(error))
        if not isinstance(error, ResultCatalogError):
            messagebox.showerror("无法载入图片", str(error), parent=self.root)

    def _open_path(self, path: Path) -> None:
        try:
            resolved = path.expanduser().resolve(strict=True)
            if os.name == "nt":
                startfile = getattr(os, "startfile", None)
                if startfile is None:
                    raise OSError("Windows shell open is unavailable")
                startfile(resolved)
            elif sys_platform() == "darwin":
                subprocess.Popen(["open", str(resolved)])
            else:
                subprocess.Popen(["xdg-open", str(resolved)])
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法打开路径", str(exc), parent=self.root)


def sys_platform() -> str:
    """Tiny seam used by tests without importing a platform GUI helper."""

    import sys

    return sys.platform
