from __future__ import annotations

import json
import os
import queue
import re
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Final

from PIL import Image, UnidentifiedImageError

from .aliyun import AliyunImageEditProvider, ImageEditCancelled
from .errors import ImageEditProviderError
from .models import (
    ImageEditRequest,
    ImageEditValidationError,
    validate_image_edit_request,
)

_MAX_IMAGE_BYTES: Final = 10 * 1024 * 1024
_MAX_TASKS: Final = 200
_DEFAULT_WORKERS: Final = 4
_TERMINAL_STATUSES: Final = frozenset({"succeeded", "failed", "cancelled"})
_ACTIVE_STATUSES: Final = frozenset(
    {"queued", "uploading", "generating", "downloading", "cancelling"}
)
_SUPPORTED_FORMATS: Final = frozenset({"JPEG", "PNG", "BMP", "TIFF", "WEBP", "GIF"})
_FORMAT_ALIASES: Final = {"MPO": "JPEG"}
_FORMAT_SUFFIXES: Final = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "BMP": ".bmp",
    "TIFF": ".tiff",
    "WEBP": ".webp",
    "GIF": ".gif",
}
_UNSAFE_FILENAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f]+")


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class ImageEditServiceError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details


@dataclass(slots=True)
class ImageEditTask:
    id: str
    source_name: str
    source_path: Path
    output_path: Path
    request: ImageEditRequest
    submitted_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    status: str = "queued"
    stage: str = "queued"
    error: dict[str, object] | None = None
    result: dict[str, object] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)


ProviderFactory = Callable[[], AliyunImageEditProvider]


class ImageEditTaskManager:
    """Owns independent, concurrent image-edit tasks and their saved results."""

    def __init__(
        self,
        *,
        config_home: Path,
        api_key_getter: Callable[[], str],
        api_url_getter: Callable[[], str],
        provider_factory: ProviderFactory | None = None,
        worker_count: int = _DEFAULT_WORKERS,
    ) -> None:
        if worker_count < 1:
            raise ValueError("worker_count must be positive")
        self._root = config_home.expanduser().resolve() / "image-edit"
        self._staging = self._root / "staging"
        self._settings_path = self._root / "settings.json"
        self._api_key_getter = api_key_getter
        self._api_url_getter = api_url_getter
        self._provider_factory = provider_factory
        self._worker_count = worker_count
        self._tasks: dict[str, ImageEditTask] = {}
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._started = False
        self._closed = False
        self._staging.mkdir(parents=True, exist_ok=True)
        self._remove_stale_staging_files()

    def get_settings(self) -> dict[str, object]:
        output_directory = self._load_output_directory()
        return {
            "configured": output_directory is not None,
            "output_directory": str(output_directory) if output_directory else None,
        }

    def update_settings(self, payload: Mapping[str, object]) -> dict[str, object]:
        unknown = set(payload) - {"output_directory"}
        if unknown:
            raise ImageEditServiceError(
                "unknown_fields",
                "输出目录设置包含不支持的字段。",
                details={"fields": sorted(unknown)},
            )
        raw_directory = payload.get("output_directory")
        if not isinstance(raw_directory, str) or not raw_directory.strip():
            raise ImageEditServiceError(
                "invalid_output_directory", "请选择有效的输出目录。"
            )
        candidate = Path(raw_directory.strip()).expanduser()
        if not candidate.is_absolute():
            raise ImageEditServiceError(
                "invalid_output_directory", "输出目录必须使用绝对路径。"
            )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ImageEditServiceError(
                "invalid_output_directory", "输出目录不存在或无法访问。"
            ) from exc
        if not resolved.is_dir():
            raise ImageEditServiceError(
                "invalid_output_directory", "所选路径不是目录。"
            )
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._settings_path.with_name(
            f".{self._settings_path.name}.{secrets.token_hex(6)}.tmp"
        )
        body = json.dumps(
            {"output_directory": str(resolved)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            with temporary.open("xb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._settings_path)
        except OSError as exc:
            raise ImageEditServiceError(
                "settings_write_failed",
                "无法保存输出目录设置，请检查应用数据目录权限。",
                status_code=500,
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return self.get_settings()

    def submit_multipart(self, content_type: str, body: bytes) -> dict[str, object]:
        if self._closed:
            raise ImageEditServiceError(
                "service_unavailable", "图片编辑服务正在关闭。", status_code=503
            )
        api_key = self._api_key_getter().strip()
        if not api_key:
            raise ImageEditServiceError(
                "credentials_not_configured",
                "尚未配置 DashScope API Key，请先在设置中配置凭证。",
                status_code=409,
            )
        output_directory = self._load_output_directory()
        if output_directory is None:
            raise ImageEditServiceError(
                "output_directory_required",
                "请先选择生成结果的输出目录。",
                status_code=409,
            )
        metadata, source_name, source_bytes = _parse_multipart(content_type, body)
        try:
            request = validate_image_edit_request(metadata)
        except ImageEditValidationError as exc:
            raise ImageEditServiceError(exc.code, str(exc)) from exc
        source_format = _validate_source_image(source_bytes)
        source_suffix = _FORMAT_SUFFIXES[source_format]
        task_id = uuid.uuid4().hex
        source_path = self._staging / f"{task_id}{source_suffix}"
        try:
            with source_path.open("xb") as handle:
                handle.write(source_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            source_path.unlink(missing_ok=True)
            raise ImageEditServiceError(
                "staging_write_failed",
                "无法暂存原图，请检查应用数据目录空间与权限。",
                status_code=500,
            ) from exc
        output_path = output_directory / _output_filename(source_name, task_id)
        task = ImageEditTask(
            id=task_id,
            source_name=_display_source_name(source_name),
            source_path=source_path,
            output_path=output_path,
            request=request,
        )
        with self._lock:
            if self._closed:
                source_path.unlink(missing_ok=True)
                raise ImageEditServiceError(
                    "service_unavailable", "图片编辑服务正在关闭。", status_code=503
                )
            self._tasks[task.id] = task
            self._trim_tasks_locked()
            self._start_workers_locked()
            self._queue.put(task.id)
            return self._view_locked(task)

    def list_tasks(
        self, *, active: bool | None = None, limit: int = 100
    ) -> dict[str, object]:
        if not 1 <= limit <= 200:
            raise ImageEditServiceError(
                "invalid_params", "limit 必须在 1 到 200 之间。"
            )
        with self._lock:
            tasks = sorted(
                self._tasks.values(), key=lambda task: task.submitted_at, reverse=True
            )
            if active is not None:
                tasks = [
                    task
                    for task in tasks
                    if (task.status in _ACTIVE_STATUSES) == active
                ]
            selected = tasks[:limit]
            return {
                "tasks": [self._view_locked(task) for task in selected],
                "count": len(selected),
                "total_count": len(tasks),
            }

    def get_task(self, task_id: str) -> dict[str, object]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ImageEditServiceError(
                    "image_edit_task_not_found",
                    "图片编辑任务不存在。",
                    status_code=404,
                )
            return self._view_locked(task)

    def cancel_task(self, task_id: str) -> dict[str, object]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise ImageEditServiceError(
                    "image_edit_task_not_found",
                    "图片编辑任务不存在。",
                    status_code=404,
                )
            if task.status in _TERMINAL_STATUSES:
                return self._view_locked(task)
            task.cancel_event.set()
            if task.status == "queued":
                self._mark_cancelled_locked(task)
                task.source_path.unlink(missing_ok=True)
            else:
                task.status = "cancelling"
                task.stage = "cancelling"
            return self._view_locked(task)

    def abandon_all(self, payload: Mapping[str, object]) -> dict[str, object]:
        unknown = set(payload) - {"confirm"}
        if unknown or payload.get("confirm") is not True:
            raise ImageEditServiceError(
                "confirmation_required",
                "放弃未保存结果前必须明确确认。",
            )
        with self._lock:
            abandoned: list[str] = []
            for task in self._tasks.values():
                if task.status not in _ACTIVE_STATUSES:
                    continue
                task.cancel_event.set()
                previous_status = task.status
                self._mark_cancelled_locked(task)
                if previous_status == "queued":
                    task.source_path.unlink(missing_ok=True)
                abandoned.append(task.id)
            return {"abandoned_task_ids": sorted(abandoned), "count": len(abandoned)}

    def active_task_ids(self) -> list[str]:
        with self._lock:
            return sorted(
                task.id
                for task in self._tasks.values()
                if task.status in _ACTIVE_STATUSES
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for task in self._tasks.values():
                if task.status in _ACTIVE_STATUSES:
                    task.cancel_event.set()
                    self._mark_cancelled_locked(task)
            workers = list(self._workers)
        for _worker in workers:
            self._queue.put(None)
        for worker in workers:
            worker.join(timeout=0.25)

    def _load_output_directory(self) -> Path | None:
        try:
            payload = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        value = payload.get("output_directory") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        return resolved if resolved.is_dir() else None

    def _start_workers_locked(self) -> None:
        if self._started:
            return
        self._started = True
        for index in range(self._worker_count):
            worker = threading.Thread(
                target=self._worker,
                name=f"zvec-image-edit-{index + 1}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _worker(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                if task_id is None:
                    return
                self._execute(task_id)
            finally:
                self._queue.task_done()

    def _execute(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status != "queued":
                return
            task.started_at = _utc_now()
            task.status = "uploading"
            task.stage = "uploading"

        try:
            provider = (
                self._provider_factory()
                if self._provider_factory is not None
                else AliyunImageEditProvider(
                    api_key=self._api_key_getter(),
                    api_url=self._api_url_getter(),
                )
            )
            result = provider.edit(
                task.source_path,
                task.request,
                task.output_path,
                progress=lambda stage: self._set_stage(task.id, stage),
                cancel_event=task.cancel_event,
            )
        except ImageEditCancelled:
            with self._lock:
                self._mark_cancelled_locked(task)
        except ImageEditProviderError as exc:
            with self._lock:
                if task.cancel_event.is_set():
                    self._mark_cancelled_locked(task)
                else:
                    task.status = "failed"
                    task.stage = "failed"
                    task.finished_at = _utc_now()
                    task.error = {
                        "code": exc.details.code,
                        "message": exc.details.message,
                        "retryable": exc.details.retryable,
                        "request_id": exc.details.request_id or None,
                        "status_code": exc.details.status_code,
                    }
        except (OSError, TimeoutError):
            with self._lock:
                if task.cancel_event.is_set():
                    self._mark_cancelled_locked(task)
                else:
                    failed_stage = task.stage
                    task.status = "failed"
                    task.stage = "failed"
                    task.finished_at = _utc_now()
                    if failed_stage == "downloading":
                        message = (
                            "生成结果无法下载或保存，请检查网络、"
                            "输出目录空间与权限后重试。"
                        )
                    else:
                        message = "无法连接阿里云图片编辑服务，请检查网络后重试。"
                    task.error = {
                        "code": "image_edit_transport_failed",
                        "message": message,
                        "retryable": True,
                        "request_id": None,
                        "status_code": None,
                    }
        except Exception:
            with self._lock:
                if task.cancel_event.is_set():
                    self._mark_cancelled_locked(task)
                else:
                    task.status = "failed"
                    task.stage = "failed"
                    task.finished_at = _utc_now()
                    task.error = {
                        "code": "image_edit_failed",
                        "message": "图片生成失败，请稍后重试。",
                        "retryable": True,
                        "request_id": None,
                        "status_code": None,
                    }
        else:
            with self._lock:
                if task.cancel_event.is_set() or task.status == "cancelled":
                    self._mark_cancelled_locked(task)
                    task.output_path.unlink(missing_ok=True)
                else:
                    task.status = "succeeded"
                    task.stage = "succeeded"
                    task.finished_at = _utc_now()
                    task.result = dict(result)
        finally:
            task.source_path.unlink(missing_ok=True)
            with self._lock:
                if task.status == "cancelled":
                    task.output_path.unlink(missing_ok=True)

    def _set_stage(self, task_id: str, stage: str) -> None:
        if stage not in {"uploading", "generating", "downloading"}:
            return
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.cancel_event.is_set():
                return
            task.status = stage
            task.stage = stage

    @staticmethod
    def _mark_cancelled_locked(task: ImageEditTask) -> None:
        task.status = "cancelled"
        task.stage = "cancelled"
        task.finished_at = task.finished_at or _utc_now()
        task.error = None

    def _view_locked(self, task: ImageEditTask) -> dict[str, object]:
        return {
            "id": task.id,
            "source_name": task.source_name,
            "submitted_at": task.submitted_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "status": task.status,
            "stage": task.stage,
            "model": task.request.model,
            "prompt": task.request.prompt,
            "negative_prompt": task.request.negative_prompt,
            "size": task.request.size,
            "seed": task.request.seed,
            "prompt_extend": task.request.prompt_extend,
            "error": dict(task.error) if task.error else None,
            "result": dict(task.result) if task.result else None,
        }

    def _trim_tasks_locked(self) -> None:
        if len(self._tasks) <= _MAX_TASKS:
            return
        terminal = sorted(
            (
                task
                for task in self._tasks.values()
                if task.status in _TERMINAL_STATUSES
            ),
            key=lambda task: task.submitted_at,
        )
        for task in terminal[: max(0, len(self._tasks) - _MAX_TASKS)]:
            self._tasks.pop(task.id, None)

    def _remove_stale_staging_files(self) -> None:
        for child in self._staging.iterdir():
            if child.is_file():
                child.unlink(missing_ok=True)


def _parse_multipart(
    content_type: str, body: bytes
) -> tuple[dict[str, object], str, bytes]:
    if not content_type.casefold().startswith("multipart/form-data;"):
        raise ImageEditServiceError(
            "unsupported_media_type",
            "图片编辑任务必须使用 multipart/form-data。",
            status_code=415,
        )
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    )
    if not message.is_multipart():
        raise ImageEditServiceError("invalid_multipart", "上传表单格式无效。")
    metadata: dict[str, object] | None = None
    source_name = "source-image"
    source_bytes: bytes | None = None
    for part in message.iter_parts():
        field_name = part.get_param("name", header="content-disposition")
        if field_name == "metadata" and metadata is None:
            raw = part.get_payload(decode=True) or b""
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ImageEditServiceError(
                    "invalid_metadata", "任务参数必须是有效的 UTF-8 JSON。"
                ) from exc
            if not isinstance(decoded, dict):
                raise ImageEditServiceError(
                    "invalid_metadata", "任务参数必须是 JSON 对象。"
                )
            metadata = decoded
        elif field_name == "file" and source_bytes is None:
            source_name = part.get_filename() or source_name
            source_bytes = part.get_payload(decode=True) or b""
        else:
            raise ImageEditServiceError(
                "invalid_multipart", "上传表单只能包含 metadata 和单个图片文件。"
            )
    if metadata is None or source_bytes is None:
        raise ImageEditServiceError(
            "invalid_multipart", "上传表单缺少任务参数或图片文件。"
        )
    if not source_bytes:
        raise ImageEditServiceError("empty_image", "上传的图片文件为空。")
    if len(source_bytes) > _MAX_IMAGE_BYTES:
        raise ImageEditServiceError(
            "image_too_large", "单张图片不能超过 10 MiB。", status_code=413
        )
    return metadata, source_name, source_bytes


def _validate_source_image(content: bytes) -> str:
    from io import BytesIO

    try:
        with Image.open(BytesIO(content)) as image:
            image_format = image.format or ""
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ImageEditServiceError(
            "invalid_image", "图片格式无效或文件已损坏。"
        ) from exc
    image_format = _FORMAT_ALIASES.get(image_format, image_format)
    if image_format not in _SUPPORTED_FORMATS:
        raise ImageEditServiceError(
            "unsupported_image_format",
            "仅支持 JPEG、PNG、BMP、TIFF、WebP 和 GIF 图片。",
        )
    return image_format


def _display_source_name(value: str) -> str:
    name = Path(value.replace("\\", "/")).name.strip()
    return name[:255] or "source-image"


def _output_filename(source_name: str, task_id: str) -> str:
    stem = Path(_display_source_name(source_name)).stem
    stem = _UNSAFE_FILENAME.sub("_", stem).strip(" ._")[:80] or "image"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stem}-qwen-edit-{timestamp}-{task_id[:8]}.png"


__all__ = [
    "ImageEditServiceError",
    "ImageEditTaskManager",
]
