"""Responsive tkinter widgets used by the pure-Python desktop client."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from tkinter import ttk
from typing import Any, Generic, Protocol, TypeVar

from PIL import Image, ImageTk

from .gallery_layout import calculate_gallery_layout
from .theme import DEFAULT_THEME, DesktopTheme


class GalleryItem(Protocol):
    """Minimum result fields consumed by the reusable gallery widgets."""

    @property
    def name(self) -> str: ...

    @property
    def display_path(self) -> Path: ...

    @property
    def rank(self) -> int: ...

    @property
    def library_name(self) -> str: ...

    @property
    def match_state(self) -> str: ...


ImageLoaderCallable = Callable[[Path, tuple[int, int], str], Image.Image]
ImageCompletion = Callable[[Image.Image | None, str | None], None]
GalleryItemT = TypeVar("GalleryItemT", bound=GalleryItem)


class ImageTaskDispatcher:
    """Decode and resize images away from tkinter's single UI thread."""

    def __init__(
        self,
        root: tk.Misc,
        loader: ImageLoaderCallable,
        *,
        workers: int | None = None,
    ) -> None:
        self._root = root
        self._loader = loader
        self._executor = ThreadPoolExecutor(
            max_workers=workers or max(2, min(4, os.cpu_count() or 2)),
            thread_name_prefix="zvec-image",
        )
        self._completed: queue.SimpleQueue[
            tuple[ImageCompletion, Image.Image | None, str | None]
        ] = queue.SimpleQueue()
        self._state_lock = threading.Lock()
        self._closed = False
        self._poll_handle: str | None = self._root.after(25, self._drain)

    def request(
        self,
        path: Path,
        size: tuple[int, int],
        mode: str,
        completion: ImageCompletion,
    ) -> Future[Image.Image] | None:
        """Schedule a render and return its cancellable worker future.

        A canvas cancels superseded queued work when its path, fit mode or size
        changes.  Running Pillow decodes finish safely in the worker, while
        queued resize bursts are discarded before consuming memory or CPU.
        """

        with self._state_lock:
            if self._closed:
                return None
            try:
                future = self._executor.submit(self._loader, path, size, mode)
            except RuntimeError:
                # ``close`` may race a request made by a widget being destroyed.
                return None
        future.add_done_callback(lambda value: self._collect(value, completion))
        return future

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        if self._poll_handle is not None:
            with suppress(tk.TclError):
                self._root.after_cancel(self._poll_handle)
            self._poll_handle = None
        self._executor.shutdown(wait=False, cancel_futures=True)
        while True:
            try:
                _completion, image, _error = self._completed.get_nowait()
            except queue.Empty:
                break
            if image is not None:
                image.close()

    def _collect(
        self,
        future: Future[Image.Image],
        completion: ImageCompletion,
    ) -> None:
        try:
            image = future.result()
        except CancelledError:
            return
        except Exception as exc:  # Worker failures must never terminate the UI.
            with self._state_lock:
                if not self._closed:
                    self._completed.put((completion, None, str(exc)))
        else:
            with self._state_lock:
                if self._closed:
                    image.close()
                else:
                    self._completed.put((completion, image, None))

    def _drain(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            # The callback associated with the previous handle is executing;
            # do not leave a stale identifier for ``close`` to cancel.
            self._poll_handle = None
        for _ in range(128):
            try:
                completion, image, error = self._completed.get_nowait()
            except queue.Empty:
                break
            try:
                completion(image, error)
            except tk.TclError:
                # A result can arrive after a page or fullscreen window closed.
                if image is not None:
                    image.close()
                continue
            except Exception:
                # Rendering callbacks are a UI boundary.  A corrupt Tk image,
                # allocation failure or page-specific bug must not stop queue
                # polling and freeze every later thumbnail.
                if image is not None:
                    image.close()
                continue
        with self._state_lock:
            if self._closed:
                return
            with suppress(tk.TclError):
                self._poll_handle = self._root.after(25, self._drain)


class AsyncImageCanvas(tk.Canvas):
    """Canvas that asynchronously renders either a contained or covered image."""

    def __init__(
        self,
        master: tk.Misc,
        dispatcher: ImageTaskDispatcher,
        *,
        mode: str = "contain",
        background: str = DEFAULT_THEME.surface_muted,
        placeholder: str = "暂无图片",
        **kwargs: Any,
    ) -> None:
        self._validate_mode(mode)
        super().__init__(
            master,
            background=background,
            highlightthickness=0,
            borderwidth=0,
            **kwargs,
        )
        self._dispatcher = dispatcher
        self._mode = mode
        self._placeholder = placeholder
        self._path: Path | None = None
        self._render_token = 0
        self._photo: ImageTk.PhotoImage | None = None
        self._resize_handle: str | None = None
        self._pending_future: Future[Image.Image] | None = None
        self._destroyed = False
        self.bind("<Configure>", self._on_resize, add=True)
        self.bind("<Destroy>", self._on_destroy, add=True)
        self._draw_placeholder(placeholder)

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._validate_mode(mode)
        if self._mode == mode:
            return
        self._mode = mode
        self._schedule_render(immediate=True)

    def set_image(self, path: str | Path | None) -> None:
        self._photo = None
        self._path = Path(path).expanduser() if path else None
        if self._path is None:
            self._invalidate_render()
            self._draw_placeholder(self._placeholder)
            return
        self._schedule_render(immediate=True)

    def _on_resize(self, _event: tk.Event[tk.Misc]) -> None:
        self._schedule_render(immediate=False)

    def _schedule_render(self, *, immediate: bool) -> None:
        if self._destroyed:
            return
        token = self._invalidate_render()
        delay = 0 if immediate else 90
        self._resize_handle = self.after(delay, lambda: self._request_render(token))

    def _invalidate_render(self) -> int:
        """Invalidate every older path/mode/size request and return a new token."""

        self._render_token += 1
        if self._resize_handle is not None:
            with suppress(tk.TclError):
                self.after_cancel(self._resize_handle)
            self._resize_handle = None
        if self._pending_future is not None:
            self._pending_future.cancel()
            self._pending_future = None
        return self._render_token

    def _request_render(self, token: int) -> None:
        if self._destroyed or token != self._render_token:
            return
        self._resize_handle = None
        path = self._path
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        if path is None or width < 8 or height < 8:
            return
        size = (width, height)
        mode = self._mode
        self._draw_placeholder("正在载入…")

        def completed(image: Image.Image | None, error: str | None) -> None:
            try:
                current = (
                    token == self._render_token
                    and not self._destroyed
                    and path == self._path
                    and mode == self._mode
                    and size
                    == (max(1, self.winfo_width()), max(1, self.winfo_height()))
                    and bool(self.winfo_exists())
                )
            except tk.TclError:
                current = False
            if not current:
                if image is not None:
                    image.close()
                return
            self._pending_future = None
            if image is None:
                self._draw_placeholder("图片不可用", error)
                return
            try:
                photo = ImageTk.PhotoImage(image)
            except Exception as exc:
                self._draw_placeholder("图片无法显示", str(exc))
                return
            finally:
                image.close()
            self._photo = photo
            self.delete("all")
            self.create_image(
                width // 2,
                height // 2,
                image=self._photo,
                anchor="center",
            )

        future = self._dispatcher.request(path, size, mode, completed)
        if future is None:
            if token == self._render_token and not self._destroyed:
                self._draw_placeholder("图片加载已停止")
            return
        if token != self._render_token or self._destroyed:
            future.cancel()
            return
        self._pending_future = future

    def _on_destroy(self, event: tk.Event[tk.Misc]) -> None:
        if event.widget is not self or self._destroyed:
            return
        self._destroyed = True
        self._invalidate_render()
        self._photo = None

    @staticmethod
    def _validate_mode(mode: str) -> None:
        if mode not in {"contain", "cover"}:
            raise ValueError("Image mode must be 'contain' or 'cover'.")

    def _draw_placeholder(self, message: str, detail: str | None = None) -> None:
        self.delete("all")
        self.create_text(
            max(1, self.winfo_width()) // 2,
            max(1, self.winfo_height()) // 2,
            text=message,
            fill=DEFAULT_THEME.text_muted,
            font=("Microsoft YaHei UI", 10),
            width=max(80, self.winfo_width() - 24),
            justify="center",
        )
        if detail:
            self.configure(takefocus=True)
            self.bind("<FocusIn>", lambda _event: None, add=True)
            self._last_error = detail


class ResponsiveGallery(ttk.Frame, Generic[GalleryItemT]):
    """A 15-item responsive gallery that scrolls when cards cannot fit."""

    def __init__(
        self,
        master: tk.Misc,
        dispatcher: ImageTaskDispatcher,
        *,
        theme: DesktopTheme = DEFAULT_THEME,
        max_columns: int = 5,
        **kwargs: Any,
    ) -> None:
        super().__init__(master, **kwargs)
        self._dispatcher = dispatcher
        self._theme = theme
        self._max_columns = max_columns
        self._items: list[GalleryItemT] = []
        self._selected_index: int | None = None
        self._on_select: Callable[[int, GalleryItemT], None] | None = None
        self._on_open: Callable[[int, GalleryItemT], None] | None = None
        self._layout_signature: tuple[int, int, int] | None = None
        self._resize_handle: str | None = None
        self._scrollbar_visible = False
        self._configured_columns = 0

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(
            self,
            background=theme.surface,
            highlightthickness=0,
            borderwidth=0,
        )
        self._scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self._canvas.yview,
        )
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._scrollbar.grid(row=0, column=1, sticky="ns")
        self._scrollbar.grid_remove()
        self._content = tk.Frame(self._canvas, background=theme.surface)
        self._content_window = self._canvas.create_window(
            (0, 0), window=self._content, anchor="nw"
        )
        self._content.bind("<Configure>", self._update_scroll_region, add=True)
        self._canvas.bind("<Configure>", self._on_canvas_resize, add=True)
        self._canvas.bind("<MouseWheel>", self._on_mouse_wheel, add=True)
        self._content.bind("<MouseWheel>", self._on_mouse_wheel, add=True)

    def set_items(
        self,
        items: Sequence[GalleryItemT],
        *,
        on_select: Callable[[int, GalleryItemT], None],
        on_open: Callable[[int, GalleryItemT], None],
    ) -> None:
        self._items = list(items)
        self._on_select = on_select
        self._on_open = on_open
        self._selected_index = None
        self._layout_signature = None
        self._rebuild()
        self._canvas.yview_moveto(0.0)

    def select(self, index: int) -> None:
        if index < 0 or index >= len(self._items):
            return
        self._selected_index = index
        self._update_selection_styles()
        if self._on_select is not None:
            self._on_select(index, self._items[index])

    def _on_canvas_resize(self, event: tk.Event[tk.Misc]) -> None:
        self._canvas.itemconfigure(self._content_window, width=max(1, event.width))
        if self._resize_handle is not None:
            self.after_cancel(self._resize_handle)
        self._resize_handle = self.after(80, self._rebuild)

    def _update_scroll_region(self, _event: tk.Event[tk.Misc]) -> None:
        bounds = self._canvas.bbox("all")
        self._canvas.configure(scrollregion=bounds)
        content_height = 0 if bounds is None else bounds[3] - bounds[1]
        needs_scrollbar = content_height > self._canvas.winfo_height() + 2
        if needs_scrollbar == self._scrollbar_visible:
            return
        self._scrollbar_visible = needs_scrollbar
        if needs_scrollbar:
            self._scrollbar.grid()
        else:
            self._scrollbar.grid_remove()
            self._canvas.yview_moveto(0.0)

    def _on_mouse_wheel(self, event: tk.Event[tk.Misc]) -> str:
        delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta * 3, "units")
        return "break"

    def _layout(self) -> tuple[int, int, int]:
        layout = calculate_gallery_layout(
            max(1, self._canvas.winfo_width()),
            max(1, self._canvas.winfo_height()),
            len(self._items),
            max_columns=self._max_columns,
        )
        return layout.columns, layout.card_width, layout.card_height

    def _rebuild(self) -> None:
        self._resize_handle = None
        signature = self._layout()
        if signature == self._layout_signature:
            return
        self._layout_signature = signature
        for child in self._content.winfo_children():
            child.destroy()
        if not self._items:
            label = tk.Label(
                self._content,
                text="暂无图片\n可打开图片文件夹，或载入最近一次搜索结果",
                background=self._theme.surface,
                foreground=self._theme.text_muted,
                font=("Microsoft YaHei UI", 11),
                justify="center",
            )
            label.pack(fill="both", expand=True, padx=24, pady=80)
            return

        columns, card_width, card_height = signature
        self._configure_grid_columns(columns)
        for index, item in enumerate(self._items):
            row, column = divmod(index, columns)
            card = self._create_card(index, item, card_width, card_height)
            card.grid(row=row, column=column, padx=4, pady=4, sticky="nsew")
        self._update_selection_styles()

    def _configure_grid_columns(self, columns: int) -> None:
        """Apply the current grid and clear columns left by a wider viewport."""

        for column in range(max(columns, self._configured_columns)):
            active = column < columns
            self._content.grid_columnconfigure(
                column,
                weight=1 if active else 0,
                uniform="gallery" if active else "",
            )
        self._configured_columns = columns

    def _create_card(
        self,
        index: int,
        item: GalleryItemT,
        width: int,
        height: int,
    ) -> tk.Frame:
        card = tk.Frame(
            self._content,
            name=f"card_{index}",
            width=width,
            height=height,
            background=self._theme.surface,
            highlightthickness=1,
            highlightbackground=self._theme.border,
            highlightcolor=self._theme.primary,
            cursor="hand2",
        )
        card.grid_propagate(False)
        card.pack_propagate(False)
        # Reserve enough vertical space for both title and subtitle when the
        # normal 5x3 layout compresses cards to roughly 135 logical pixels.
        # ``expand=True`` gives any remaining room back to the thumbnail.
        image_height = max(64, int(height * 0.55))
        image = AsyncImageCanvas(
            card,
            self._dispatcher,
            mode="cover",
            height=image_height,
            background=self._theme.surface_muted,
            placeholder="载入中…",
        )
        image.pack(fill="both", expand=True)
        image.set_image(item.display_path)
        caption = tk.Frame(card, background=self._theme.surface)
        caption.pack(fill="x", padx=8, pady=(4, 5))
        title = tk.Label(
            caption,
            text=f"#{item.rank}  {self._ellipsize(Path(item.name).stem, 10)}",
            anchor="w",
            background=self._theme.surface,
            foreground=self._theme.text,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        title.pack(fill="x")
        subtitle = tk.Label(
            caption,
            text=self._subtitle(item),
            anchor="w",
            background=self._theme.surface,
            foreground=self._theme.text_muted,
            font=("Microsoft YaHei UI", 8),
        )
        subtitle.pack(fill="x", pady=(3, 0))

        def select(_event: tk.Event[tk.Misc]) -> str:
            self.select(index)
            return "break"

        def open_item(_event: tk.Event[tk.Misc]) -> str:
            self.select(index)
            if self._on_open is not None:
                self._on_open(index, item)
            return "break"

        for widget in (card, image, caption, title, subtitle):
            widget.bind("<Button-1>", select, add=True)
            widget.bind("<Double-Button-1>", open_item, add=True)
            widget.bind("<MouseWheel>", self._on_mouse_wheel, add=True)
        return card

    def _update_selection_styles(self) -> None:
        for index, child in enumerate(self._content.winfo_children()):
            if not isinstance(child, tk.Frame):
                continue
            selected = index == self._selected_index
            child.configure(
                highlightbackground=(
                    self._theme.primary if selected else self._theme.border
                ),
                highlightthickness=2 if selected else 1,
            )

    @staticmethod
    def _subtitle(item: GalleryItem) -> str:
        state = {
            "high": "高相关",
            "possible": "可能",
            "weak": "低置信",
        }.get(item.match_state, "图片")
        return " · ".join(value for value in (item.library_name, state) if value)

    @staticmethod
    def _ellipsize(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: max(1, limit - 1)] + "…"


class FullscreenImageViewer(tk.Toplevel):
    """Borderless full-screen image viewer with cover/contain switching."""

    def __init__(
        self,
        master: tk.Misc,
        dispatcher: ImageTaskDispatcher,
        items: Sequence[GalleryItem],
        index: int,
        *,
        theme: DesktopTheme = DEFAULT_THEME,
    ) -> None:
        super().__init__(master)
        self._items = list(items)
        self._index = max(0, min(index, len(self._items) - 1))
        self._theme = theme
        self._mode = "cover"
        self._fullscreen = True
        self._cursor_handle: str | None = None
        self.configure(background=theme.fullscreen)
        self.attributes("-fullscreen", True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self._surface = AsyncImageCanvas(
            self,
            dispatcher,
            mode=self._mode,
            background=theme.fullscreen,
            placeholder="正在载入图片…",
        )
        self._surface.pack(fill="both", expand=True)

        controls = tk.Frame(self, background="#1B2230", padx=8, pady=6)
        controls.place(relx=0.5, y=14, anchor="n")
        self._title = tk.Label(
            controls,
            background="#1B2230",
            foreground="white",
            font=("Microsoft YaHei UI", 10, "bold"),
            width=38,
            anchor="w",
        )
        self._title.pack(side="left", padx=(3, 10))
        self._mode_button = tk.Button(
            controls,
            text="完整显示",
            command=self.toggle_mode,
            relief="flat",
            background="#39445A",
            foreground="white",
            activebackground="#4A5872",
            activeforeground="white",
            cursor="hand2",
            takefocus=False,
        )
        self._mode_button.pack(side="left", padx=3)
        close = tk.Button(
            controls,
            text="关闭  Esc",
            command=self.destroy,
            relief="flat",
            background="#A33D45",
            foreground="white",
            activebackground="#BC4B55",
            activeforeground="white",
            cursor="hand2",
            takefocus=False,
        )
        close.pack(side="left", padx=(3, 0))

        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<KeyPress-space>", self._on_toggle_mode)
        self.bind("<F11>", lambda _event: self.toggle_fullscreen())
        self.bind("<Left>", lambda _event: self.previous())
        self.bind("<Right>", lambda _event: self.next())
        self.bind("<Double-Button-1>", lambda _event: self.destroy())
        self.bind("<Motion>", self._show_cursor, add=True)
        self.focus_force()
        self._surface.focus_set()
        self._show_current()
        self._show_cursor()

    def _on_toggle_mode(self, _event: tk.Event[tk.Misc]) -> str:
        self.toggle_mode()
        return "break"

    def toggle_mode(self) -> None:
        self._mode = "contain" if self._mode == "cover" else "cover"
        self._surface.set_mode(self._mode)
        self._mode_button.configure(
            text="铺满屏幕" if self._mode == "contain" else "完整显示"
        )

    def toggle_fullscreen(self) -> None:
        self._fullscreen = not self._fullscreen
        self.attributes("-fullscreen", self._fullscreen)
        if not self._fullscreen:
            self.geometry("1100x760")

    def previous(self) -> None:
        if not self._items:
            return
        self._index = (self._index - 1) % len(self._items)
        self._show_current()

    def next(self) -> None:
        if not self._items:
            return
        self._index = (self._index + 1) % len(self._items)
        self._show_current()

    def _show_current(self) -> None:
        if not self._items:
            self.destroy()
            return
        item = self._items[self._index]
        self._title.configure(
            text=f"{self._index + 1} / {len(self._items)}   {item.name}"
        )
        self._surface.set_image(item.display_path)

    def _show_cursor(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        self.configure(cursor="arrow")
        if self._cursor_handle is not None:
            self.after_cancel(self._cursor_handle)
        self._cursor_handle = self.after(2200, lambda: self.configure(cursor="none"))

    def destroy(self) -> None:
        if self._cursor_handle is not None:
            with suppress(tk.TclError):
                self.after_cancel(self._cursor_handle)
            self._cursor_handle = None
        with suppress(tk.TclError):
            super().destroy()
