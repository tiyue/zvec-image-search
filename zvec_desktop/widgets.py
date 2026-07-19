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
                # A result can arrive after a page or application window closed.
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

    # Search libraries are dominated by portraits and cosplay photography.
    # Showing the complete composition is more useful than filling every pixel
    # at the cost of cropping a face, costume or pose out of the thumbnail.
    _THUMBNAIL_MODE = "contain"
    _MIN_CAPTION_HEIGHT = 40
    _MAX_CAPTION_HEIGHT = 44
    _LAYOUT_RETRY_MS = 25

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
        self._hovered_index: int | None = None
        self._on_select: Callable[[int, GalleryItemT], None] | None = None
        self._layout_signature: tuple[int, int, int, bool] | None = None
        self._resize_handle: str | None = None
        self._scrollbar_visible = False
        self._desired_scrollbar_visible = False
        self._configured_columns = 0
        self._card_chrome: dict[
            int,
            tuple[tk.Frame, tk.Frame, tk.Label, tk.Label],
        ] = {}

        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(
            self,
            background=theme.surface_subtle,
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
        self._content = tk.Frame(self._canvas, background=theme.surface_subtle)
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
    ) -> None:
        self._items = list(items)
        self._on_select = on_select
        self._selected_index = None
        self._hovered_index = None
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
        # The content frame can report the previous grid's requested height
        # while Tk is coalescing Configure events.  On fast CI machines that
        # transient value used to leave a scrollbar visible after the final
        # 5x3 layout already fitted.  The deterministic layout result is the
        # authoritative source for scrollbar visibility.
        self._set_scrollbar_visible(self._desired_scrollbar_visible)

    def _set_scrollbar_visible(self, visible: bool) -> None:
        if visible == self._scrollbar_visible:
            return
        self._scrollbar_visible = visible
        if visible:
            self._scrollbar.grid()
        else:
            self._scrollbar.grid_remove()
            self._canvas.yview_moveto(0.0)

    def _on_mouse_wheel(self, event: tk.Event[tk.Misc]) -> str:
        delta = -1 if event.delta > 0 else 1
        self._canvas.yview_scroll(delta * 3, "units")
        return "break"

    def _layout(self) -> tuple[int, int, int, bool]:
        layout = calculate_gallery_layout(
            max(1, self._canvas.winfo_width()),
            max(1, self._canvas.winfo_height()),
            len(self._items),
            max_columns=self._max_columns,
        )
        return (
            layout.columns,
            layout.card_width,
            layout.card_height,
            layout.scroll_required,
        )

    def _rebuild(self) -> None:
        self._resize_handle = None
        # Do not build a temporary one-column grid before Tk has assigned a
        # meaningful viewport.  It wastes fifteen thumbnail requests and can
        # leak a stale scrollbar state into the first rendered frame.
        if self._canvas.winfo_width() < 16 or self._canvas.winfo_height() < 16:
            self._layout_signature = None
            # A short timer lets Tk process the pending native geometry event.
            # Re-queuing immediately with ``after_idle`` can starve that event
            # when callers populate a gallery before the first window update.
            self._resize_handle = self.after(
                self._LAYOUT_RETRY_MS,
                self._rebuild,
            )
            return
        signature = self._layout()
        self._desired_scrollbar_visible = signature[3]
        self._set_scrollbar_visible(self._desired_scrollbar_visible)
        if signature == self._layout_signature:
            return
        self._layout_signature = signature
        self._hovered_index = None
        self._card_chrome.clear()
        for child in self._content.winfo_children():
            child.destroy()
        if not self._items:
            label = tk.Label(
                self._content,
                text="暂无图片\n可打开图片文件夹，或载入最近一次搜索结果",
                background=self._theme.surface_subtle,
                foreground=self._theme.text_muted,
                font=("Microsoft YaHei UI", 11),
                justify="center",
            )
            label.pack(fill="both", expand=True, padx=24, pady=80)
            return

        columns, card_width, card_height, _scroll_required = signature
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
            highlightbackground=self._theme.border_strong,
            highlightcolor=self._theme.primary,
            cursor="hand2",
        )
        card.grid_propagate(False)
        card.pack_propagate(False)
        # Keep caption chrome deliberately compact so the photograph remains
        # the primary visual even in the normal 5x3 grid.  The fixed caption
        # height also prevents long collection names from shrinking portraits.
        caption_height = self._caption_height(height)
        # Include the card border plus both widgets' vertical padding in the
        # budget; otherwise Tk may satisfy the canvas request by clipping the
        # caption by a few pixels in the shortest 5x3 layout.
        image_height = max(48, height - caption_height - 12)
        image = AsyncImageCanvas(
            card,
            self._dispatcher,
            mode=self._THUMBNAIL_MODE,
            height=image_height,
            background=self._theme.preview,
            placeholder="载入中…",
        )
        image.pack(fill="both", expand=True, padx=3, pady=(3, 0))
        image.set_image(item.display_path)
        rank_badge = tk.Label(
            image,
            text=f"{item.rank:02d}",
            background=self._theme.navigation,
            foreground="white",
            font=(self._theme.typography.family, 7, "bold"),
            padx=5,
            pady=2,
        )
        rank_badge.place(x=7, y=7, anchor="nw")
        caption = tk.Frame(
            card,
            name="caption",
            height=caption_height,
            background=self._theme.surface,
        )
        caption.pack(fill="x", padx=6, pady=(2, 3))
        caption.pack_propagate(False)
        title = tk.Label(
            caption,
            name="title",
            text=self._ellipsize(
                Path(item.name).stem,
                max(12, min(24, width // 7)),
            ),
            anchor="w",
            background=self._theme.surface,
            foreground=self._theme.text,
            font=(self._theme.typography.family, 8, "bold"),
        )
        title.pack(fill="x")
        subtitle = tk.Label(
            caption,
            name="subtitle",
            text=self._subtitle(item),
            anchor="w",
            background=self._theme.surface,
            foreground=self._theme.text_muted,
            font=(self._theme.typography.family, 7),
        )
        subtitle.pack(fill="x")

        self._card_chrome[index] = (card, caption, title, subtitle)

        def select(_event: tk.Event[tk.Misc]) -> str:
            self.select(index)
            return "break"

        def hover(_event: tk.Event[tk.Misc]) -> None:
            self._set_hovered_index(index)

        def leave(_event: tk.Event[tk.Misc]) -> None:
            self._clear_hovered_index(index)

        for widget in (card, image, rank_badge, caption, title, subtitle):
            widget.bind("<Button-1>", select, add=True)
            widget.bind("<MouseWheel>", self._on_mouse_wheel, add=True)
            widget.bind("<Enter>", hover, add=True)
            widget.bind("<Leave>", leave, add=True)
        return card

    def _update_selection_styles(self) -> None:
        for index, (card, caption, title, subtitle) in self._card_chrome.items():
            background, border, thickness = self._card_visual_style(index)
            try:
                card.configure(
                    background=background,
                    highlightbackground=border,
                    highlightthickness=thickness,
                )
                caption.configure(background=background)
                title.configure(
                    background=background,
                    foreground=(
                        self._theme.primary
                        if index == self._selected_index
                        else self._theme.text
                    ),
                )
                subtitle.configure(background=background)
            except tk.TclError:
                # A resize can destroy old cards while a queued hover event is
                # still being delivered.  The rebuilt grid receives its final
                # style at the end of ``_rebuild``.
                continue

    def _set_hovered_index(self, index: int) -> None:
        if self._hovered_index == index:
            return
        self._hovered_index = index
        self._update_selection_styles()

    def _clear_hovered_index(self, index: int) -> None:
        if self._hovered_index != index:
            return
        self._hovered_index = None
        self._update_selection_styles()

    def _card_visual_style(self, index: int) -> tuple[str, str, int]:
        """Return card colours with selection taking precedence over hover."""

        if index == self._selected_index:
            return self._theme.selection, self._theme.primary, 2
        if index == self._hovered_index:
            return self._theme.surface_subtle, self._theme.focus, 1
        return self._theme.surface, self._theme.border_strong, 1

    @classmethod
    def _caption_height(cls, card_height: int) -> int:
        """Scale the compact two-line caption within a narrow safe range."""

        return max(
            cls._MIN_CAPTION_HEIGHT,
            min(cls._MAX_CAPTION_HEIGHT, round(card_height * 0.3)),
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
