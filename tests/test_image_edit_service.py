from __future__ import annotations

import json
import threading
import time
from io import BytesIO
from pathlib import Path

from PIL import Image

from image_vector_service.image_edit.errors import translated_provider_error
from image_vector_service.image_edit.service import ImageEditTaskManager


def _png_bytes(color: tuple[int, int, int] = (12, 34, 56)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (20, 16), color).save(output, format="PNG")
    return output.getvalue()


def _mpo_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (20, 16), (12, 34, 56)).save(
        output,
        format="MPO",
        save_all=True,
        append_images=[Image.new("RGB", (4, 4), (65, 43, 21))],
    )
    return output.getvalue()


def _multipart(
    metadata: dict[str, object],
    content: bytes,
    *,
    filename: str = "source.png",
    media_type: str = "image/png",
) -> tuple[str, bytes]:
    boundary = "----zvec-image-edit-test"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="metadata"\r\n\r\n',
            json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\n'
            ).encode(),
            f"Content-Type: {media_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return f"multipart/form-data; boundary={boundary}", body


def _request() -> dict[str, object]:
    return {
        "model": "qwen-image-edit-plus",
        "prompt": "把背景换成海边",
        "negative_prompt": "模糊",
        "size": "1024*1024",
        "seed": 42,
        "prompt_extend": True,
    }


def _wait_terminal(manager: ImageEditTaskManager, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = manager.get_task(task_id)
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            return task
        time.sleep(0.01)
    raise AssertionError("image-edit task did not finish")


class _SuccessfulProvider:
    def edit(self, source_path, request, output_path, *, progress, cancel_event):
        assert source_path.read_bytes() == _png_bytes()
        assert request.prompt == "把背景换成海边"
        progress("generating")
        progress("downloading")
        output_path.write_bytes(_png_bytes((90, 80, 70)))
        return {
            "output_path": str(output_path),
            "output_filename": output_path.name,
            "request_id": "request-1",
            "width": 20,
            "height": 16,
            "image_count": 1,
        }


def test_settings_persist_and_success_means_the_png_is_already_saved(
    tmp_path: Path,
) -> None:
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    manager = ImageEditTaskManager(
        config_home=tmp_path / "config",
        api_key_getter=lambda: "secret",
        api_url_getter=lambda: "https://dashscope.aliyuncs.com/api/v1/x",
        provider_factory=_SuccessfulProvider,
        worker_count=1,
    )
    try:
        settings = manager.update_settings({"output_directory": str(output_directory)})
        assert settings == {
            "configured": True,
            "output_directory": str(output_directory.resolve()),
        }
        content_type, body = _multipart(_request(), _png_bytes())
        submitted = manager.submit_multipart(content_type, body)
        terminal = _wait_terminal(manager, str(submitted["id"]))

        assert terminal["status"] == "succeeded"
        result = terminal["result"]
        assert isinstance(result, dict)
        saved = Path(str(result["output_path"]))
        assert saved.parent == output_directory.resolve()
        assert saved.read_bytes() == _png_bytes((90, 80, 70))
        assert not list((tmp_path / "config" / "image-edit" / "staging").iterdir())
    finally:
        manager.close()

    restarted = ImageEditTaskManager(
        config_home=tmp_path / "config",
        api_key_getter=lambda: "secret",
        api_url_getter=lambda: "https://dashscope.aliyuncs.com/api/v1/x",
        provider_factory=_SuccessfulProvider,
    )
    try:
        assert restarted.get_settings()["output_directory"] == str(
            output_directory.resolve()
        )
    finally:
        restarted.close()


def test_jpeg_with_mpf_auxiliary_frame_is_accepted_as_jpeg(tmp_path: Path) -> None:
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    source = _mpo_bytes()

    with Image.open(BytesIO(source)) as image:
        assert image.format == "MPO"

    class SuccessfulMpoProvider:
        def edit(self, source_path, request, output_path, *, progress, cancel_event):
            del request, cancel_event
            assert source_path.suffix == ".jpg"
            assert source_path.read_bytes() == source
            progress("generating")
            progress("downloading")
            output_path.write_bytes(_png_bytes())
            return {
                "output_path": str(output_path),
                "output_filename": output_path.name,
            }

    manager = ImageEditTaskManager(
        config_home=tmp_path / "config",
        api_key_getter=lambda: "secret",
        api_url_getter=lambda: "https://dashscope.aliyuncs.com/api/v1/x",
        provider_factory=SuccessfulMpoProvider,
        worker_count=1,
    )
    manager.update_settings({"output_directory": str(output_directory)})
    content_type, body = _multipart(
        _request(), source, filename="source.JPG", media_type="image/jpeg"
    )
    try:
        submitted = manager.submit_multipart(content_type, body)
        terminal = _wait_terminal(manager, str(submitted["id"]))
        assert terminal["status"] == "succeeded"
    finally:
        manager.close()


def test_multiple_tasks_can_run_at_the_same_time(tmp_path: Path) -> None:
    output_directory = tmp_path / "output"
    output_directory.mkdir()
    all_started = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    active = 0

    class BlockingProvider:
        def edit(self, source_path, request, output_path, *, progress, cancel_event):
            del source_path, request
            nonlocal active
            progress("generating")
            with state_lock:
                active += 1
                if active == 2:
                    all_started.set()
            assert release.wait(2)
            output_path.write_bytes(_png_bytes())
            return {
                "output_path": str(output_path),
                "output_filename": output_path.name,
            }

    manager = ImageEditTaskManager(
        config_home=tmp_path / "config",
        api_key_getter=lambda: "secret",
        api_url_getter=lambda: "https://dashscope.aliyuncs.com/api/v1/x",
        provider_factory=BlockingProvider,
        worker_count=2,
    )
    manager.update_settings({"output_directory": str(output_directory)})
    content_type, body = _multipart(_request(), _png_bytes())
    first = manager.submit_multipart(content_type, body)
    second = manager.submit_multipart(content_type, body)
    try:
        assert all_started.wait(2), "both tasks should enter the provider concurrently"
        assert len(manager.active_task_ids()) == 2
        release.set()
        assert _wait_terminal(manager, str(first["id"]))["status"] == "succeeded"
        assert _wait_terminal(manager, str(second["id"]))["status"] == "succeeded"
    finally:
        release.set()
        manager.close()


def test_provider_failure_keeps_prompt_and_exposes_translated_reason(
    tmp_path: Path,
) -> None:
    output_directory = tmp_path / "output"
    output_directory.mkdir()

    class FailingProvider:
        def edit(self, source_path, request, output_path, *, progress, cancel_event):
            del source_path, request, output_path, progress, cancel_event
            raise translated_provider_error(
                "Throttling", "rate limited", status_code=429
            )

    manager = ImageEditTaskManager(
        config_home=tmp_path / "config",
        api_key_getter=lambda: "secret",
        api_url_getter=lambda: "https://dashscope.aliyuncs.com/api/v1/x",
        provider_factory=FailingProvider,
        worker_count=1,
    )
    manager.update_settings({"output_directory": str(output_directory)})
    content_type, body = _multipart(_request(), _png_bytes())
    submitted = manager.submit_multipart(content_type, body)
    try:
        terminal = _wait_terminal(manager, str(submitted["id"]))
        assert terminal["status"] == "failed"
        assert terminal["prompt"] == "把背景换成海边"
        error = terminal["error"]
        assert isinstance(error, dict)
        assert "限流" in str(error["message"])
        assert error["retryable"] is True
    finally:
        manager.close()
