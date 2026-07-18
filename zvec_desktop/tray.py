"""Thread-safe system-tray events for the pure-Python desktop client."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from PIL import Image, ImageDraw


class TrayIconError(RuntimeError):
    """The platform tray icon could not be created or started."""


class TrayEventKind(str, Enum):
    SHOW = "show"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class TrayEvent:
    kind: TrayEventKind


class TrayIconBackend(Protocol):
    def run_detached(self) -> None: ...

    def stop(self) -> None: ...

    def notify(self, message: str, title: str | None = None) -> None: ...


TrayIconFactory = Callable[
    [str, Image.Image, Callable[[], None], Callable[[], None]], TrayIconBackend
]


class TrayIconService:
    """Own a tray icon while delivering all actions through a safe queue.

    ``pystray`` invokes menu callbacks on its own thread.  Those callbacks only
    enqueue :class:`TrayEvent`; the Tk window drains them from ``root.after`` and
    therefore never receives cross-thread widget calls.
    """

    def __init__(
        self,
        *,
        title: str = "Zvec 图片库",
        icon_factory: TrayIconFactory | None = None,
    ) -> None:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be non-empty")
        self._title = title.strip()
        self._factory = icon_factory or _default_icon_factory
        self._events: queue.SimpleQueue[TrayEvent] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._icon: TrayIconBackend | None = None
        self._running = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> None:
        """Start once; repeated calls are harmless."""

        with self._lock:
            if self._running:
                return
            image = create_tray_image()
            try:
                icon = self._factory(
                    self._title,
                    image,
                    lambda: self._enqueue(TrayEventKind.SHOW),
                    lambda: self._enqueue(TrayEventKind.EXIT),
                )
                icon.run_detached()
            except Exception as exc:
                image.close()
                raise TrayIconError(f"无法启动系统托盘图标：{exc}") from exc
            self._icon = icon
            self._running = True

    def stop(self) -> None:
        """Remove the tray icon without generating an exit event."""

        with self._lock:
            icon = self._icon
            self._icon = None
            self._running = False
        if icon is not None:
            try:
                icon.stop()
            except Exception as exc:
                raise TrayIconError(f"无法关闭系统托盘图标：{exc}") from exc

    def notify(self, message: str, *, title: str | None = None) -> None:
        """Show a best-effort native notification while the icon is active."""

        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be non-empty")
        with self._lock:
            icon = self._icon if self._running else None
        if icon is None:
            return
        try:
            icon.notify(message.strip(), title.strip() if title else None)
        except Exception as exc:
            raise TrayIconError(f"无法显示系统通知：{exc}") from exc

    def drain_events(
        self,
        callback: Callable[[TrayEvent], None] | None = None,
        *,
        max_events: int = 20,
    ) -> tuple[TrayEvent, ...]:
        if isinstance(max_events, bool) or not isinstance(max_events, int):
            raise ValueError("max_events must be a positive integer")
        if max_events < 1:
            raise ValueError("max_events must be a positive integer")
        events: list[TrayEvent] = []
        for _ in range(max_events):
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        if callback is not None:
            for event in events:
                callback(event)
        return tuple(events)

    def _enqueue(self, kind: TrayEventKind) -> None:
        with self._lock:
            if not self._running:
                return
        self._events.put(TrayEvent(kind))


def create_tray_image(size: int = 64) -> Image.Image:
    """Render a crisp small icon without another packaged bitmap payload."""

    if isinstance(size, bool) or not isinstance(size, int) or not 16 <= size <= 256:
        raise ValueError("size must be an integer between 16 and 256")
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(2, size // 16)
    radius = max(3, size // 6)
    draw.rounded_rectangle(
        (margin, margin, size - margin - 1, size - margin - 1),
        radius=radius,
        fill=(79, 91, 230, 255),
    )
    photo_left = size * 19 // 100
    photo_top = size * 22 // 100
    photo_right = size * 67 // 100
    photo_bottom = size * 67 // 100
    line_width = max(2, size // 16)
    draw.rounded_rectangle(
        (photo_left, photo_top, photo_right, photo_bottom),
        radius=max(2, size // 16),
        outline="white",
        width=line_width,
    )
    draw.ellipse(
        (
            size * 52 // 100,
            size * 50 // 100,
            size * 77 // 100,
            size * 75 // 100,
        ),
        outline="white",
        width=line_width,
    )
    draw.line(
        (
            size * 72 // 100,
            size * 70 // 100,
            size * 84 // 100,
            size * 82 // 100,
        ),
        fill="white",
        width=line_width,
    )
    return image


def _default_icon_factory(
    title: str,
    image: Image.Image,
    on_show: Callable[[], None],
    on_exit: Callable[[], None],
) -> TrayIconBackend:
    try:
        import pystray
    except ImportError as exc:
        raise TrayIconError("缺少 pystray 运行组件。") from exc

    def show(_icon: object, _item: object) -> None:
        on_show()

    def exit_application(_icon: object, _item: object) -> None:
        on_exit()

    menu = pystray.Menu(
        pystray.MenuItem("显示主窗口", show, default=True),
        pystray.MenuItem("退出", exit_application),
    )
    return pystray.Icon("zvec-image-search", image, title, menu)


__all__ = [
    "TrayEvent",
    "TrayEventKind",
    "TrayIconBackend",
    "TrayIconError",
    "TrayIconFactory",
    "TrayIconService",
    "create_tray_image",
]
