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
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, cast

from PIL import Image

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
    FullscreenImageViewer,
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
        self.theme = theme
        self.root = tk.Tk(className="ZvecDesktop")
        self.root.title("Zvec 图片库")
        screen_width = max(720, self.root.winfo_screenwidth())
        screen_height = max(500, self.root.winfo_screenheight())
        initial_width = min(1280, max(720, screen_width - 80))
        initial_height = min(900, max(500, screen_height - 120))
        self.root.geometry(f"{initial_width}x{initial_height}")
        self.root.minsize(720, 500)
        self.root.configure(background=theme.window)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
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

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        with suppress(tk.TclError):
            style.theme_use("clam")
        style.configure("App.TFrame", background=self.theme.window)
        style.configure("Surface.TFrame", background=self.theme.surface)
        style.configure(
            "Title.TLabel",
            background=self.theme.window,
            foreground=self.theme.text,
            font=("Microsoft YaHei UI", 16, "bold"),
        )
        style.configure(
            "Section.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text,
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure(
            "Muted.TLabel",
            background=self.theme.surface,
            foreground=self.theme.text_muted,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Primary.TButton",
            background=self.theme.primary,
            foreground="white",
            borderwidth=0,
            padding=(13, 8),
        )
        style.map(
            "Primary.TButton",
            background=[("active", self.theme.primary_hover)],
            foreground=[("disabled", "#D7DCEF")],
        )
        style.configure("Quiet.TButton", padding=(10, 7))

    def _build_window(self) -> None:
        shell = ttk.Frame(self.root, style="App.TFrame", padding=(18, 14, 18, 10))
        shell.pack(fill="both", expand=True)
        shell.rowconfigure(1, weight=1)
        shell.columnconfigure(0, weight=1)

        header = ttk.Frame(shell, style="App.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Zvec 图片库", style="Title.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._source_text = tk.StringVar(value="正在读取图片…")
        ttk.Label(
            header,
            textvariable=self._source_text,
            style="Muted.TLabel",
        ).grid(row=0, column=1, sticky="w", padx=(14, 12))
        self._latest_button = ttk.Button(
            header,
            text="载入最新结果",
            style="Quiet.TButton",
            command=self.load_latest,
        )
        self._latest_button.grid(row=0, column=2, padx=(0, 7))
        self._folder_button = ttk.Button(
            header,
            text="打开图片文件夹",
            style="Primary.TButton",
            command=self.choose_folder,
        )
        self._folder_button.grid(row=0, column=3, padx=(0, 7))
        self._exit_button = ttk.Button(
            header,
            text="退出",
            style="Quiet.TButton",
            command=self.close,
        )
        self._exit_button.grid(row=0, column=4)

        self._notebook = ttk.Notebook(shell)
        self._notebook.grid(row=1, column=0, sticky="nsew")
        search_page = ttk.Frame(self._notebook, style="App.TFrame")
        task_page = ttk.Frame(self._notebook, style="App.TFrame")
        self._organize_page = ttk.Frame(self._notebook, style="App.TFrame")
        self._settings_page = ttk.Frame(self._notebook, style="App.TFrame")
        self._notebook.add(search_page, text="图片搜索")
        self._notebook.add(task_page, text="图库任务")
        self._notebook.add(self._organize_page, text="智能整理")
        self._notebook.add(self._settings_page, text="设置")
        self._notebook.bind("<<NotebookTabChanged>>", self._on_main_tab_changed)
        search_page.rowconfigure(1, weight=1)
        search_page.columnconfigure(0, weight=1)
        self._build_search(search_page)

        body = ttk.Panedwindow(search_page, orient="horizontal")
        body.grid(row=1, column=0, sticky="nsew")
        gallery_card = ttk.Frame(body, style="Surface.TFrame", padding=12)
        preview_card = ttk.Frame(body, style="Surface.TFrame", padding=12)
        body.add(gallery_card, weight=5)
        body.add(preview_card, weight=3)
        self._build_gallery(gallery_card)
        self._build_preview(preview_card)
        self._build_library_tasks(task_page)
        self._build_organize_page(self._organize_page)
        self._build_settings_page(self._settings_page)

        status_frame = ttk.Frame(shell, style="App.TFrame")
        status_frame.grid(row=2, column=0, sticky="ew", pady=(9, 0))
        status_frame.columnconfigure(0, weight=1)
        self._status_text = tk.StringVar(value="准备就绪")
        ttk.Label(
            status_frame,
            textvariable=self._status_text,
            background=self.theme.window,
            foreground=self.theme.text_muted,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            status_frame,
            text="双击图片全屏 · 空格切换完整/铺满 · Esc 关闭",
            background=self.theme.window,
            foreground=self.theme.text_muted,
        ).grid(row=0, column=1, sticky="e")

    def _build_organize_page(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)
        toolbar = ttk.Frame(parent, style="Surface.TFrame", padding=(12, 10))
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        toolbar.columnconfigure(3, weight=1)
        ttk.Label(toolbar, text="智能整理", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 14)
        )
        ttk.Label(toolbar, text="图库", style="Muted.TLabel").grid(
            row=0, column=1, sticky="e", padx=(0, 5)
        )
        self._organize_library_var = tk.StringVar(value="")
        self._organize_library_combo = ttk.Combobox(
            toolbar,
            textvariable=self._organize_library_var,
            state="readonly",
            width=28,
        )
        self._organize_library_combo.grid(row=0, column=2, sticky="w", padx=(0, 12))
        self._organize_library_combo.bind(
            "<<ComboboxSelected>>", self._on_organize_library_selected
        )
        ttk.Label(
            toolbar,
            text="支持本次新增、角色、作品、动作、神态和待审核状态筛选",
            style="Muted.TLabel",
        ).grid(row=0, column=3, sticky="w")

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

    def _on_main_tab_changed(self, _event: tk.Event[Any] | None = None) -> None:
        self._assert_ui_thread()
        selected = self._notebook.select()
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
        card = ttk.Frame(parent, style="Surface.TFrame", padding=(11, 9))
        card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        card.columnconfigure(0, weight=1)

        connection = ttk.Frame(card, style="Surface.TFrame")
        connection.grid(row=0, column=0, sticky="ew")
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="后台", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._backend_status_text = tk.StringVar(value="等待启动")
        ttk.Label(
            connection,
            textvariable=self._backend_status_text,
            style="Section.TLabel",
        ).grid(row=0, column=1, sticky="w", padx=(7, 10))
        self._backend_error_text = tk.StringVar(value="")
        ttk.Label(
            connection,
            textvariable=self._backend_error_text,
            style="Muted.TLabel",
        ).grid(row=0, column=2, sticky="e", padx=(0, 10))
        self._retry_backend_button = ttk.Button(
            connection,
            text="重试",
            style="Quiet.TButton",
            command=self.start_backend,
        )
        self._retry_backend_button.grid(row=0, column=3, padx=(0, 12))
        ttk.Label(connection, text="API Key", style="Muted.TLabel").grid(
            row=0, column=4, padx=(0, 5)
        )
        self._api_key_var = tk.StringVar(value="")
        self._api_key_entry = ttk.Entry(
            connection,
            textvariable=self._api_key_var,
            show="●",
            width=25,
        )
        self._api_key_entry.grid(row=0, column=5, padx=(0, 7))
        self._api_key_entry.bind(
            "<Return>", lambda _event: self.configure_api_key(), add=True
        )
        self._remember_key_var = tk.BooleanVar(value=False)
        self._remember_key_check = ttk.Checkbutton(
            connection,
            text="记住密钥",
            variable=self._remember_key_var,
        )
        self._remember_key_check.grid(row=0, column=6, padx=(0, 7))
        if not self._credential_store.persistent:
            self._remember_key_check.configure(state="disabled")
        self._configure_key_button = ttk.Button(
            connection,
            text="应用密钥",
            style="Quiet.TButton",
            command=self.configure_api_key,
            state="disabled",
        )
        self._configure_key_button.grid(row=0, column=7)
        self._configure_key_buttons = [self._configure_key_button]

        query = ttk.Frame(card, style="Surface.TFrame")
        query.grid(row=1, column=0, sticky="ew", pady=(8, 0))
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
        )
        self._query_entry.grid(row=0, column=1, sticky="ew", padx=(0, 7))
        self._query_entry.bind("<Return>", lambda _event: self.start_search(), add=True)
        self._choose_query_image_button = ttk.Button(
            query,
            text="选择查询图片",
            style="Quiet.TButton",
            command=self.choose_query_image,
        )
        self._choose_query_image_button.grid(row=0, column=2, padx=(0, 5))
        self._clear_query_image_button = ttk.Button(
            query,
            text="清除图片",
            style="Quiet.TButton",
            command=self.clear_query_image,
            state="disabled",
        )
        self._clear_query_image_button.grid(row=0, column=3, padx=(0, 7))
        self._search_button = ttk.Button(
            query,
            text="搜索",
            style="Primary.TButton",
            command=self.start_search,
            state="disabled",
        )
        self._search_button.grid(row=0, column=4, padx=(0, 5))
        self._cancel_search_button = ttk.Button(
            query,
            text="取消",
            style="Quiet.TButton",
            command=self.cancel_search,
            state="disabled",
        )
        self._cancel_search_button.grid(row=0, column=5)

        options = ttk.Frame(card, style="Surface.TFrame")
        options.grid(row=2, column=0, sticky="ew", pady=(7, 0))
        options.columnconfigure(3, weight=1)
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
        ttk.Label(
            options,
            textvariable=self._query_image_var,
            style="Muted.TLabel",
        ).grid(row=0, column=2, sticky="nw", padx=(0, 12))
        self._search_progress_text = tk.StringVar(value="默认返回 15 张，候选 50 张")
        ttk.Label(
            options,
            textvariable=self._search_progress_text,
            style="Muted.TLabel",
        ).grid(row=0, column=3, sticky="ne")

    def _build_library_tasks(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        controls = ttk.Frame(parent, style="Surface.TFrame", padding=(12, 10))
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        controls.columnconfigure(3, weight=1)
        ttk.Label(controls, text="图库任务", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12)
        )
        ttk.Label(controls, text="图库", style="Muted.TLabel").grid(
            row=0, column=1, sticky="e", padx=(0, 5)
        )
        self._task_library_var = tk.StringVar(value="")
        self._task_library_combo = ttk.Combobox(
            controls,
            textvariable=self._task_library_var,
            state="readonly",
            width=26,
        )
        self._task_library_combo.grid(row=0, column=2, sticky="w", padx=(0, 12))
        ttk.Label(
            controls,
            text="每次任务只处理当前选择的一个图库",
            style="Muted.TLabel",
        ).grid(row=0, column=3, sticky="w")
        ttk.Label(controls, text="API Key", style="Muted.TLabel").grid(
            row=0, column=4, sticky="e", padx=(10, 5)
        )
        task_key_entry = ttk.Entry(
            controls,
            textvariable=self._api_key_var,
            show="●",
            width=20,
        )
        task_key_entry.grid(row=0, column=5, padx=(0, 7))
        task_key_entry.bind(
            "<Return>", lambda _event: self.configure_api_key(), add=True
        )
        task_remember = ttk.Checkbutton(
            controls,
            text="记住密钥",
            variable=self._remember_key_var,
        )
        task_remember.grid(row=0, column=6, padx=(0, 7))
        if not self._credential_store.persistent:
            task_remember.configure(state="disabled")
        task_key_button = ttk.Button(
            controls,
            text="应用密钥",
            style="Quiet.TButton",
            command=self.configure_api_key,
            state="disabled",
        )
        task_key_button.grid(row=0, column=7)
        self._configure_key_buttons.append(task_key_button)

        ttk.Label(
            controls,
            text="本次新增图片标签",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(9, 0))
        self._manual_tags_var = tk.StringVar(value="")
        self._manual_tags_entry = ttk.Entry(
            controls,
            textvariable=self._manual_tags_var,
        )
        self._manual_tags_entry.grid(
            row=1,
            column=1,
            columnspan=3,
            sticky="ew",
            padx=(0, 12),
            pady=(9, 0),
        )
        ttk.Label(
            controls,
            text="逗号或换行分隔；不会应用到已有图片",
            style="Muted.TLabel",
        ).grid(row=1, column=4, sticky="w", pady=(9, 0))

        options = ttk.Frame(controls, style="Surface.TFrame")
        options.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(9, 0))
        self._task_recursive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options,
            text="包含子文件夹",
            variable=self._task_recursive_var,
        ).grid(row=0, column=0, padx=(0, 10))
        self._task_verify_hash_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options,
            text="校验文件哈希",
            variable=self._task_verify_hash_var,
        ).grid(row=0, column=1, padx=(0, 16))
        ttk.Label(options, text="标注模型", style="Muted.TLabel").grid(
            row=0, column=2, padx=(0, 5)
        )
        self._task_model_var = tk.StringVar(value=DEFAULT_AUTO_TAG_MODEL)
        ttk.Entry(options, textvariable=self._task_model_var, width=20).grid(
            row=0, column=3, padx=(0, 12)
        )
        ttk.Label(options, text="最多图片", style="Muted.TLabel").grid(
            row=0, column=4, padx=(0, 5)
        )
        self._task_max_images_var = tk.StringVar(value=str(DEFAULT_AUTO_TAG_MAX_IMAGES))
        ttk.Spinbox(
            options,
            from_=1,
            to=10_000,
            textvariable=self._task_max_images_var,
            width=7,
        ).grid(row=0, column=5, padx=(0, 12))
        ttk.Label(options, text="预算（元）", style="Muted.TLabel").grid(
            row=0, column=6, padx=(0, 5)
        )
        self._task_budget_var = tk.StringVar(value=str(DEFAULT_AUTO_TAG_BUDGET_CNY))
        ttk.Entry(options, textvariable=self._task_budget_var, width=8).grid(
            row=0, column=7, padx=(0, 12)
        )
        self._task_external_confirm_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options,
            text="确认图片将发送到阿里云模型",
            variable=self._task_external_confirm_var,
        ).grid(row=0, column=8)

        actions = ttk.Frame(controls, style="Surface.TFrame")
        actions.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(9, 0))
        self._task_action_buttons: list[ttk.Button] = []
        for column, (label, action, primary) in enumerate(
            (
                ("建立索引", "index", True),
                ("同步图库", "sync", False),
                ("索引并自动标注", "index_and_auto_tag", True),
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
            button.grid(row=0, column=column, padx=(0, 7))
            self._task_action_buttons.append(button)

        content = ttk.Panedwindow(parent, orient="horizontal")
        content.grid(row=1, column=0, sticky="nsew")
        center = ttk.Frame(content, style="Surface.TFrame", padding=12)
        details = ttk.Frame(content, style="Surface.TFrame", padding=12)
        content.add(center, weight=5)
        content.add(details, weight=3)

        center.rowconfigure(2, weight=1)
        center.columnconfigure(0, weight=1)
        heading = ttk.Frame(center, style="Surface.TFrame")
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        ttk.Label(heading, text="任务中心", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._task_summary_var = tk.StringVar(value="没有运行中的任务")
        ttk.Label(
            heading,
            textvariable=self._task_summary_var,
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(3, 0))
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

        details.rowconfigure(2, weight=1)
        details.columnconfigure(0, weight=1)
        ttk.Label(details, text="任务结果", style="Section.TLabel").grid(
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
            background=self.theme.surface_muted,
            foreground=self.theme.text,
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
        active = 0
        attention = 0
        for job in jobs:
            view = project_task(job)
            active += int(view.active)
            attention += int(
                view.failed_count > 0 or view.status in {"failed", "needs_attention"}
            )
            self._task_tree.insert(
                "",
                "end",
                iid=view.job_id,
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

    def clear_query_image(self) -> None:
        self._query_image_path = None
        self._query_image_var.set("未选择查询图片")
        self._clear_query_image_button.configure(state="disabled")

    def start_backend(self) -> None:
        """Start the native Python backend without blocking Tk's event loop."""

        if self._closed or self._close_pending:
            return
        state = self._runtime_controller.state
        if state in {RuntimeState.STARTING, RuntimeState.STOPPING}:
            return
        self._backend_error_text.set("")
        self._backend_status_text.set("正在启动…")
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
            self._backend_status_text.set(
                {
                    RuntimeState.STOPPED: "已停止",
                    RuntimeState.STARTING: "正在启动…",
                    RuntimeState.READY: "已就绪",
                    RuntimeState.STOPPING: "正在停止…",
                    RuntimeState.ERROR: "不可用",
                    RuntimeState.DISPOSED: "已关闭",
                }[event.state]
            )
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
            self._backend_status_text.set("已就绪")
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
            if event.operation == "list_jobs":
                self._task_refresh_future = None

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
                "请输入阿里云百炼 API Key。",
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
        self._backend_status_text.set("正在应用密钥…")
        future = self._runtime_controller.configure_credentials(api_key)

        def completed(_value: Any | None, error: BaseException | None) -> None:
            if self._closed:
                return
            self._set_configure_key_buttons_state("normal")
            if error is not None:
                self._credentials_configured = False
                self._backend_status_text.set("已就绪")
                self._backend_error_text.set(f"密钥不可用：{error}")
                return
            self._credentials_configured = True
            self._backend_status_text.set("已就绪 · 密钥已配置")
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
        ttk.Label(heading, text="图片", style="Section.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self._summary_text = tk.StringVar(value="尚未载入")
        ttk.Label(heading, textvariable=self._summary_text, style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(3, 0)
        )

        self._gallery: ResponsiveGallery[SearchResult] = ResponsiveGallery(
            parent,
            self._image_dispatcher,
            theme=self.theme,
            max_columns=5,
        )
        self._gallery.grid(row=1, column=0, sticky="nsew")

        pager = ttk.Frame(parent, style="Surface.TFrame")
        pager.grid(row=2, column=0, sticky="ew", pady=(9, 0))
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
        ttk.Label(parent, text="完整预览", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self._preview = AsyncImageCanvas(
            parent,
            self._image_dispatcher,
            mode="contain",
            background=self.theme.surface_muted,
            placeholder="请选择一张图片",
        )
        self._preview.grid(row=1, column=0, sticky="nsew")
        self._preview.bind(
            "<Double-Button-1>", lambda _event: self.open_fullscreen(), add=True
        )

        details = ttk.Frame(parent, style="Surface.TFrame")
        details.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        details.columnconfigure(0, weight=1)
        self._preview_title = tk.StringVar(value="请选择结果图片")
        ttk.Label(
            details,
            textvariable=self._preview_title,
            style="Section.TLabel",
            wraplength=360,
        ).grid(row=0, column=0, sticky="w")
        self._preview_meta = tk.StringVar(value="—")
        ttk.Label(
            details,
            textvariable=self._preview_meta,
            style="Muted.TLabel",
            wraplength=360,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(5, 0))
        self._preview_tags = tk.StringVar(value="标签：—")
        ttk.Label(
            details,
            textvariable=self._preview_tags,
            style="Muted.TLabel",
            wraplength=360,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))
        buttons = ttk.Frame(details, style="Surface.TFrame")
        buttons.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        buttons.columnconfigure(0, weight=1)
        self._fullscreen_button = ttk.Button(
            buttons,
            text="全屏查看",
            style="Primary.TButton",
            command=self.open_fullscreen,
            state="disabled",
        )
        self._fullscreen_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self._open_button = ttk.Button(
            buttons,
            text="系统打开",
            style="Quiet.TButton",
            command=self.open_selected,
            state="disabled",
        )
        self._open_button.grid(row=0, column=1, padx=5)
        self._folder_open_button = ttk.Button(
            buttons,
            text="所在文件夹",
            style="Quiet.TButton",
            command=self.open_selected_folder,
            state="disabled",
        )
        self._folder_open_button.grid(row=0, column=2, padx=(5, 0))

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
        current = page.page if page.total_pages else 0
        self._page_text.set(f"第 {current} / {page.total_pages} 页")
        self._previous_button.configure(
            state="normal" if page.has_previous else "disabled"
        )
        self._next_button.configure(state="normal" if page.has_next else "disabled")
        self._gallery.set_items(
            page.items,
            on_select=self._select_result,
            on_open=self._open_result_fullscreen,
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
        for button in (
            self._fullscreen_button,
            self._open_button,
            self._folder_open_button,
        ):
            button.configure(state="normal")

    def _clear_preview(self) -> None:
        self._selected_index = None
        self._preview.set_image(None)
        self._preview_title.set("请选择结果图片")
        self._preview_meta.set("—")
        self._preview_tags.set("标签：—")
        for button in (
            self._fullscreen_button,
            self._open_button,
            self._folder_open_button,
        ):
            button.configure(state="disabled")

    def _open_result_fullscreen(self, index: int, _item: SearchResult) -> None:
        self._selected_index = index
        self.open_fullscreen()

    def open_fullscreen(self) -> None:
        if self._selected_index is None or not self._items:
            return
        FullscreenImageViewer(
            self.root,
            self._image_dispatcher,
            self._items,
            self._selected_index,
            theme=self.theme,
        )

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
        self._gallery.set_items(
            (), on_select=self._select_result, on_open=self._open_result_fullscreen
        )
        self._summary_text.set("没有可显示的图片")
        self._source_text.set("请选择图片文件夹")
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
                os.startfile(resolved)
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
