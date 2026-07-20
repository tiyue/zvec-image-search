"""Narrow Windows-native bridge exposed to the local HTML application."""

from __future__ import annotations

import ctypes
import io
import json
import os
import secrets
import shutil
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from PIL import Image, ImageOps, UnidentifiedImageError

from .image_registry import ImageRegistry, ImageRegistryError

_MAX_BATCH_IMAGES: Final = 10_000
_MAX_CLIPBOARD_PIXELS: Final = 50_000_000
_EXPORT_HISTORY_LIMIT: Final = 64


@dataclass(slots=True)
class _ExportJob:
    job_id: str
    destination_name: str
    total: int
    status: str = "queued"
    processed: int = 0
    exported: int = 0
    skipped: int = 0
    message: str = "等待导出"
    errors: list[dict[str, str]] = field(default_factory=list)


class NativeBridge:
    """Expose only user-initiated dialogs and safe Explorer/clipboard actions."""

    def __init__(
        self,
        registry: ImageRegistry,
        *,
        request_exit: Callable[[], dict[str, Any]] | None = None,
        native_action_cooldown: float = 0.75,
    ) -> None:
        if native_action_cooldown < 0:
            raise ValueError("native_action_cooldown must be non-negative")
        self._registry = registry
        self._request_exit = request_exit
        self._native_action_cooldown = float(native_action_cooldown)
        self._window: Any = None
        self._lock = threading.RLock()
        self._active_native_actions: set[tuple[str, Path]] = set()
        self._recent_native_actions: dict[tuple[str, Path], float] = {}
        self._export_jobs: dict[str, _ExportJob] = {}

    def attach_window(self, window: Any) -> None:
        with self._lock:
            self._window = window

    def select_directory(self) -> dict[str, Any]:
        try:
            result = self._create_dialog("folder")
            path = _first_dialog_path(result)
            return {"ok": True, "path": str(path) if path is not None else None}
        except Exception as exc:
            return _bridge_error(exc)

    def select_json_file(self) -> dict[str, Any]:
        """Select one local JSON configuration file for an explicit import."""

        try:
            result = self._create_dialog("json")
            path = _first_dialog_path(result)
            if path is not None and path.suffix.casefold() != ".json":
                raise ValueError("请选择 JSON 文件。")
            return {"ok": True, "path": str(path) if path is not None else None}
        except Exception as exc:
            return _bridge_error(exc)

    def select_query_image(self) -> dict[str, Any]:
        """Return an opaque image ID; the browser never receives its path."""

        try:
            result = self._create_dialog("image")
            path = _first_dialog_path(result)
            if path is None:
                return {"ok": True, "image": None}
            metadata = self._registry.register(path)
            thumbnail_url = f"api/image/{metadata.image_id}?variant=thumbnail"
            return {
                "ok": True,
                "id": metadata.image_id,
                "image_id": metadata.image_id,
                "name": metadata.name,
                "thumbnail_url": thumbnail_url,
                "image": {
                    "id": metadata.image_id,
                    "name": metadata.name,
                    "thumbnail_url": thumbnail_url,
                },
            }
        except Exception as exc:
            return _bridge_error(exc)

    def open_image(self, image_id: str) -> dict[str, Any]:
        try:
            path = self._registry.resolve(image_id)
            if os.name != "nt":
                raise OSError("此 Preview 仅支持 Windows x64。")
            action_key = self._claim_native_action("open", path)
            if action_key is None:
                return _duplicate_action_error()
            succeeded = False
            try:
                _windows_startfile(path)
                succeeded = True
                return {"ok": True, "action": "open"}
            finally:
                self._finish_native_action(action_key, succeeded=succeeded)
        except Exception as exc:
            return _bridge_error(exc)

    def reveal_image(self, image_id: str) -> dict[str, Any]:
        try:
            path = self._registry.resolve(image_id)
            if os.name != "nt":
                raise OSError("此 Preview 仅支持 Windows x64。")
            action_key = self._claim_native_action("reveal", path)
            if action_key is None:
                return _duplicate_action_error()
            succeeded = False
            try:
                selected = _select_file_with_shell(path)
                message = None
                if not selected:
                    # Shell selection can be unavailable under unusual Explorer
                    # policies. Opening the verified parent is a safe fallback.
                    _windows_startfile(path.parent)
                    message = "无法自动选中文件，已打开所在文件夹。"
                succeeded = True
                response: dict[str, Any] = {
                    "ok": True,
                    "action": "reveal",
                    "selected": selected,
                }
                if message:
                    response["message"] = message
                return response
            finally:
                self._finish_native_action(action_key, succeeded=succeeded)
        except Exception as exc:
            return _bridge_error(exc)

    def copy_image_path(self, image_id: str) -> dict[str, Any]:
        """Legacy single-image path action retained for compatibility."""

        return self.copy_image_paths([image_id])

    def copy_image(self, image_id: str) -> dict[str, Any]:
        try:
            path = self._registry.resolve(image_id)
            _copy_windows_image(path)
            return {"ok": True, "action": "copy_image", "count": 1}
        except Exception as exc:
            return _bridge_error(exc)

    def copy_files(self, image_ids: list[str]) -> dict[str, Any]:
        try:
            paths = self._resolve_paths(image_ids)
            _copy_windows_files(paths)
            return {"ok": True, "action": "copy_files", "count": len(paths)}
        except Exception as exc:
            return _bridge_error(exc)

    def copy_image_paths(self, image_ids: list[str]) -> dict[str, Any]:
        try:
            paths = self._resolve_paths(image_ids)
            _copy_windows_clipboard("\r\n".join(str(path) for path in paths))
            return {"ok": True, "action": "copy_paths", "count": len(paths)}
        except Exception as exc:
            return _bridge_error(exc)

    def export_images(self, image_ids: list[str]) -> dict[str, Any]:
        """Choose a destination and copy selected images on a worker thread."""

        try:
            normalized_ids = _normalize_image_ids(image_ids)
            result = self._create_dialog("folder")
            destination = _first_dialog_path(result)
            if destination is None:
                return {"ok": True, "action": "export", "cancelled": True}
            destination = destination.expanduser().resolve(strict=True)
            if not destination.is_dir():
                raise NotADirectoryError("导出目标不是文件夹。")
            job_id = secrets.token_urlsafe(18)
            job = _ExportJob(
                job_id,
                destination.name or "所选文件夹",
                len(normalized_ids),
            )
            with self._lock:
                self._export_jobs[job_id] = job
                self._trim_export_jobs_locked()
            threading.Thread(
                target=self._export_worker,
                args=(job_id, normalized_ids, destination),
                name=f"zvec-export-{job_id[:8]}",
                daemon=True,
            ).start()
            return {
                "ok": True,
                "action": "export",
                "cancelled": False,
                "job": _export_job_payload(job),
            }
        except Exception as exc:
            return _bridge_error(exc)

    def export_status(self, job_id: str) -> dict[str, Any]:
        try:
            if not isinstance(job_id, str) or not job_id.strip():
                raise ValueError("导出任务编号无效。")
            with self._lock:
                job = self._export_jobs.get(job_id.strip())
                if job is None:
                    raise KeyError("导出任务不存在或已过期。")
                payload = _export_job_payload(job)
            return {"ok": True, "job": payload}
        except Exception as exc:
            return _bridge_error(exc)

    def _resolve_paths(self, image_ids: list[str]) -> tuple[Path, ...]:
        normalized_ids = _normalize_image_ids(image_ids)
        return tuple(self._registry.resolve(image_id) for image_id in normalized_ids)

    def _export_worker(
        self,
        job_id: str,
        image_ids: tuple[str, ...],
        destination: Path,
    ) -> None:
        self._update_export_job(job_id, status="running", message="正在导出")
        for image_id in image_ids:
            source: Path | None = None
            try:
                source = self._registry.resolve(image_id)
                target = _unique_export_path(destination, source.name)
                shutil.copy2(source, target)
            except Exception as exc:
                self._record_export_result(
                    job_id,
                    exported=False,
                    name=source.name if source is not None else "未知图片",
                    error=str(exc) or exc.__class__.__name__,
                )
            else:
                self._record_export_result(job_id, exported=True, name=source.name)

        with self._lock:
            job = self._export_jobs.get(job_id)
            errors = list(job.errors) if job is not None else []
        manifest_written = False
        if errors:
            manifest = destination / f"zvec-export-errors-{job_id[:8]}.json"
            try:
                manifest.write_text(
                    json.dumps(
                        {"schema_version": 1, "job_id": job_id, "errors": errors},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                manifest_written = True
            except OSError:
                manifest_written = False
        with self._lock:
            job = self._export_jobs.get(job_id)
            if job is None:
                return
            job.status = "succeeded" if job.skipped == 0 else "partial"
            if job.skipped == 0:
                job.message = f"已导出 {job.exported} 张图片"
            elif manifest_written:
                job.message = (
                    f"已导出 {job.exported} 张，跳过 {job.skipped} 张；"
                    "错误清单已写入目标文件夹"
                )
            else:
                job.message = f"已导出 {job.exported} 张，跳过 {job.skipped} 张"

    def _record_export_result(
        self,
        job_id: str,
        *,
        exported: bool,
        name: str,
        error: str = "",
    ) -> None:
        with self._lock:
            job = self._export_jobs.get(job_id)
            if job is None:
                return
            job.processed += 1
            if exported:
                job.exported += 1
            else:
                job.skipped += 1
                if len(job.errors) < 1_000:
                    job.errors.append({"name": name, "reason": error})
            job.message = f"正在导出 {job.processed} / {job.total}"

    def _update_export_job(self, job_id: str, *, status: str, message: str) -> None:
        with self._lock:
            job = self._export_jobs.get(job_id)
            if job is not None:
                job.status = status
                job.message = message

    def _trim_export_jobs_locked(self) -> None:
        if len(self._export_jobs) <= _EXPORT_HISTORY_LIMIT:
            return
        completed = [
            job_id
            for job_id, job in self._export_jobs.items()
            if job.status in {"succeeded", "partial", "failed"}
        ]
        for job_id in completed[: len(self._export_jobs) - _EXPORT_HISTORY_LIMIT]:
            self._export_jobs.pop(job_id, None)

    def exit_application(self) -> dict[str, Any]:
        callback = self._request_exit
        if callback is None:
            return {"ok": False, "error": "退出操作尚未就绪。"}
        try:
            return callback()
        except Exception as exc:
            return _bridge_error(exc)

    def _create_dialog(self, kind: str) -> Any:
        with self._lock:
            window = self._window
        if window is None:
            raise RuntimeError("窗口尚未就绪。")
        try:
            import webview
        except ImportError as exc:
            raise RuntimeError("pywebview 尚未安装。") from exc

        dialog_enum = getattr(webview, "FileDialog", None)
        if kind == "folder":
            dialog_type = (
                getattr(dialog_enum, "FOLDER", None)
                if dialog_enum is not None
                else None
            ) or getattr(webview, "FOLDER_DIALOG", None)
            if dialog_type is None:
                raise RuntimeError("当前 pywebview 不支持文件夹选择。")
            return window.create_file_dialog(dialog_type, allow_multiple=False)

        dialog_type = (
            getattr(dialog_enum, "OPEN", None) if dialog_enum is not None else None
        ) or getattr(webview, "OPEN_DIALOG", None)
        if dialog_type is None:
            raise RuntimeError("当前 pywebview 不支持文件选择。")
        file_types = (
            ("JSON 文件 (*.json)", "所有文件 (*.*)")
            if kind == "json"
            else (
                "图片 (*.jpg;*.jpeg;*.png;*.webp;*.bmp;*.gif;*.tif;*.tiff)",
                "所有文件 (*.*)",
            )
        )
        return window.create_file_dialog(
            dialog_type,
            allow_multiple=False,
            file_types=file_types,
        )

    def _claim_native_action(
        self,
        action: str,
        path: Path,
    ) -> tuple[str, Path] | None:
        key = (action, path)
        now = time.monotonic()
        with self._lock:
            expired = [
                candidate
                for candidate, timestamp in self._recent_native_actions.items()
                if now - timestamp >= self._native_action_cooldown
            ]
            for candidate in expired:
                self._recent_native_actions.pop(candidate, None)
            if key in self._active_native_actions or key in self._recent_native_actions:
                return None
            self._active_native_actions.add(key)
        return key

    def _finish_native_action(
        self,
        key: tuple[str, Path],
        *,
        succeeded: bool,
    ) -> None:
        with self._lock:
            self._active_native_actions.discard(key)
            if succeeded and self._native_action_cooldown > 0:
                self._recent_native_actions[key] = time.monotonic()


def _first_dialog_path(value: Any) -> Path | None:
    if value is None or value == "":
        return None
    if isinstance(value, (tuple, list)):
        if not value:
            return None
        value = value[0]
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("文件选择器返回了无效路径。")
    return Path(value).expanduser()


def _copy_windows_clipboard(text: str) -> None:
    if os.name != "nt":
        raise OSError("此 Preview 仅支持 Windows x64。")
    if not isinstance(text, str) or not text:
        raise ValueError("剪贴板文本不能为空。")
    unicode_text = 13
    encoded = text.encode("utf-16-le") + b"\x00\x00"
    _set_windows_clipboard_data(unicode_text, encoded)


def _copy_windows_files(paths: tuple[Path, ...]) -> None:
    if os.name != "nt":
        raise OSError("此 Preview 仅支持 Windows x64。")
    if not paths:
        raise ValueError("没有可复制的图片。")
    # DROPFILES followed by a double-NUL-terminated UTF-16 path list.
    dropfiles = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    payload = dropfiles + ("\0".join(str(path) for path in paths) + "\0\0").encode(
        "utf-16-le"
    )
    _set_windows_clipboard_data(15, payload)  # CF_HDROP


def _copy_windows_image(path: Path) -> None:
    if os.name != "nt":
        raise OSError("此 Preview 仅支持 Windows x64。")
    try:
        with Image.open(path) as source:
            width, height = source.size
            if width < 1 or height < 1 or width * height > _MAX_CLIPBOARD_PIXELS:
                raise ValueError("图片过大，建议使用“复制文件”保留原图。")
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = io.BytesIO()
            image.save(output, format="BMP")
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise OSError("无法把图片写入剪贴板。") from exc
    bitmap = output.getvalue()
    if len(bitmap) <= 14:
        raise OSError("图片剪贴板数据无效。")
    _set_windows_clipboard_data(8, bitmap[14:])  # CF_DIB


def _set_windows_clipboard_data(format_id: int, payload: bytes) -> None:
    if os.name != "nt":
        raise OSError("此 Preview 仅支持 Windows x64。")
    if not payload:
        raise ValueError("剪贴板内容不能为空。")
    user32 = _windows_dll("user32")
    kernel32 = _windows_dll("kernel32")
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    global_moveable = 0x0002
    if not user32.OpenClipboard(None):
        raise OSError("无法打开 Windows 剪贴板。")
    handle = None
    transferred = False
    try:
        if not user32.EmptyClipboard():
            raise OSError("无法清空 Windows 剪贴板。")
        handle = kernel32.GlobalAlloc(global_moveable, len(payload))
        if not handle:
            raise MemoryError("无法分配剪贴板内存。")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise OSError("无法锁定剪贴板内存。")
        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(format_id, handle):
            raise OSError("无法写入 Windows 剪贴板。")
        transferred = True
    finally:
        user32.CloseClipboard()
        if handle and not transferred:
            kernel32.GlobalFree(handle)


def _select_file_with_shell(path: Path) -> bool:
    """Ask Windows Shell to open Explorer and select a Unicode/long-path file."""

    if os.name != "nt":
        raise OSError("此 Preview 仅支持 Windows x64。")
    shell32 = _windows_dll("shell32")
    ole32 = _windows_dll("ole32")
    shell32.SHParseDisplayName.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    shell32.SHParseDisplayName.restype = ctypes.c_long
    shell32.ILClone.argtypes = [ctypes.c_void_p]
    shell32.ILClone.restype = ctypes.c_void_p
    shell32.ILFindLastID.argtypes = [ctypes.c_void_p]
    shell32.ILFindLastID.restype = ctypes.c_void_p
    shell32.ILRemoveLastID.argtypes = [ctypes.c_void_p]
    shell32.ILRemoveLastID.restype = ctypes.c_bool
    shell32.SHOpenFolderAndSelectItems.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
    ]
    shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    initialized = ole32.CoInitializeEx(None, 0x2)
    should_uninitialize = initialized in (0, 1)
    full_pidl = ctypes.c_void_p()
    parent_pidl = ctypes.c_void_p()
    try:
        parsed = shell32.SHParseDisplayName(
            str(path), None, ctypes.byref(full_pidl), 0, None
        )
        if parsed < 0:
            return False
        parent_pidl = ctypes.c_void_p(shell32.ILClone(full_pidl))
        if not parent_pidl.value or not shell32.ILRemoveLastID(parent_pidl):
            return False
        child_pidl = ctypes.c_void_p(shell32.ILFindLastID(full_pidl))
        if not child_pidl.value:
            return False
        children = (ctypes.c_void_p * 1)(child_pidl.value)
        return shell32.SHOpenFolderAndSelectItems(parent_pidl, 1, children, 0) >= 0
    finally:
        if parent_pidl.value:
            ole32.CoTaskMemFree(parent_pidl)
        if full_pidl.value:
            ole32.CoTaskMemFree(full_pidl)
        if should_uninitialize:
            ole32.CoUninitialize()


def _windows_startfile(path: Path) -> None:
    """Call the Windows shell opener without exposing it to Unix type stubs."""

    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError("此 Preview 仅支持 Windows x64。")
    startfile(path)


def _windows_dll(name: str) -> Any:
    """Resolve one WinDLL lazily so cross-platform static checks stay valid."""

    loader = getattr(ctypes, "windll", None)
    if loader is None:
        raise OSError("此 Preview 仅支持 Windows x64。")
    return getattr(loader, name)


def _normalize_image_ids(image_ids: list[str]) -> tuple[str, ...]:
    if not isinstance(image_ids, (list, tuple)):
        raise ValueError("图片编号列表无效。")
    if not image_ids or len(image_ids) > _MAX_BATCH_IMAGES:
        raise ValueError(f"每次请选择 1～{_MAX_BATCH_IMAGES} 张图片。")
    output: list[str] = []
    seen: set[str] = set()
    for raw in image_ids:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("图片编号列表包含无效项目。")
        image_id = raw.strip()
        if image_id not in seen:
            output.append(image_id)
            seen.add(image_id)
    return tuple(output)


def _unique_export_path(destination: Path, file_name: str) -> Path:
    safe_name = Path(file_name).name
    if not safe_name or safe_name in {".", ".."}:
        safe_name = "image"
    candidate = destination / safe_name
    if not candidate.exists():
        return candidate
    stem = Path(safe_name).stem or "image"
    suffix = Path(safe_name).suffix
    for sequence in range(2, 1_000_001):
        candidate = destination / f"{stem} ({sequence}){suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("目标文件夹中的同名文件过多。")


def _export_job_payload(job: _ExportJob) -> dict[str, Any]:
    return {
        "id": job.job_id,
        "status": job.status,
        "destination_name": job.destination_name,
        "total": job.total,
        "processed": job.processed,
        "exported": job.exported,
        "skipped": job.skipped,
        "message": job.message,
    }


def _bridge_error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ImageRegistryError):
        message = str(exc)
    else:
        message = str(exc) or exc.__class__.__name__
    return {"ok": False, "code": "native_action_failed", "error": message}


def _duplicate_action_error() -> dict[str, Any]:
    return {
        "ok": False,
        "code": "duplicate_action",
        "error": "操作正在处理中，请勿重复启动。",
    }


__all__ = ["NativeBridge"]
