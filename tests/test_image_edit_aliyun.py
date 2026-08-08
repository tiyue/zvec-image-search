from __future__ import annotations

import json
import threading
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from image_vector_service.image_edit.aliyun import (
    AliyunImageEditProvider,
    HttpResponse,
)
from image_vector_service.image_edit.errors import translated_provider_error
from image_vector_service.image_edit.models import validate_image_edit_request


def _png_bytes(color: tuple[int, int, int] = (20, 40, 60)) -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 12), color).save(output, format="PNG")
    return output.getvalue()


def test_provider_uploads_generates_and_saves_png_immediately(tmp_path: Path) -> None:
    source_path = tmp_path / "source.png"
    source_path.write_bytes(_png_bytes())
    output_path = tmp_path / "result.png"
    result_content = _png_bytes((100, 80, 60))
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def transport(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        _timeout: float,
        _maximum: int,
    ) -> HttpResponse:
        calls.append((method, url, dict(headers), body))
        if "action=getPolicy" in url:
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "data": {
                            "upload_host": "https://upload.example.invalid",
                            "upload_dir": "tmp/model-bound",
                            "oss_access_key_id": "access",
                            "signature": "signature",
                            "policy": "policy",
                            "x_oss_object_acl": "private",
                            "x_oss_forbid_overwrite": "true",
                        }
                    }
                ).encode(),
            )
        if url == "https://upload.example.invalid":
            assert body is not None
            assert body.rfind(b'name="file"') > body.find(
                b'name="success_action_status"'
            )
            assert b"tmp/model-bound/source.png" in body
            return HttpResponse(200, {}, b"")
        if url.endswith("/api/v1/services/aigc/multimodal-generation/generation"):
            request = json.loads((body or b"").decode("utf-8"))
            assert request["model"] == "qwen-image-edit-plus-2025-12-15"
            assert request["input"]["messages"][0]["content"] == [
                {"image": "oss://tmp/model-bound/source.png"},
                {"text": "把天空改成日落"},
            ]
            assert request["parameters"] == {
                "n": 1,
                "watermark": False,
                "negative_prompt": "模糊",
                "size": "1280*720",
                "seed": 7,
                "prompt_extend": True,
            }
            assert headers["X-DashScope-OssResourceResolve"] == "enable"
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "request_id": "dashscope-request-id",
                        "output": {
                            "choices": [
                                {
                                    "message": {
                                        "content": [
                                            {
                                                "image": "https://result.example.invalid/a.png"
                                            }
                                        ]
                                    }
                                }
                            ]
                        },
                        "usage": {"width": 1280, "height": 720, "image_count": 1},
                    }
                ).encode(),
            )
        if url == "https://result.example.invalid/a.png":
            return HttpResponse(200, {"content-type": "image/png"}, result_content)
        raise AssertionError(f"unexpected request: {method} {url}")

    request = validate_image_edit_request(
        {
            "model": "qwen-image-edit-plus-2025-12-15",
            "prompt": "把天空改成日落",
            "negative_prompt": "模糊",
            "size": "1280*720",
            "seed": 7,
            "prompt_extend": True,
        }
    )
    stages: list[str] = []
    result = AliyunImageEditProvider(
        api_key="secret",
        api_url="https://dashscope.aliyuncs.com/api/v1/services/embeddings/x",
        transport=transport,
    ).edit(
        source_path,
        request,
        output_path,
        progress=stages.append,
        cancel_event=threading.Event(),
    )

    assert stages == ["uploading", "generating", "downloading"]
    assert output_path.read_bytes() == result_content
    assert result["request_id"] == "dashscope-request-id"
    policy_query = parse_qs(urlsplit(calls[0][1]).query)
    assert policy_query["model"] == ["qwen-image-edit-plus-2025-12-15"]
    assert calls[0][2]["Authorization"] == "Bearer secret"


@pytest.mark.parametrize(
    ("code", "status", "expected", "retryable"),
    [
        ("InvalidApiKey", 401, "API Key 无效", False),
        ("AllocationQuota.FreeTierOnly", 403, "免费额度已用完", False),
        ("Throttling", 429, "限流", True),
        ("InvalidImageFormat", 400, "图片格式无效", False),
        ("InvalidImage.FileFormat", 400, "图片格式无效", False),
        ("Endpoint.AccessDenied", 403, "快照模型", False),
        ("result_download_failed", 403, "结果图片下载失败", True),
        ("result_download_failed", 401, "结果图片下载失败", True),
        ("ModelUnavailable", 503, "暂时不可用", True),
    ],
)
def test_provider_errors_are_translated_to_actionable_reasons(
    code: str, status: int, expected: str, retryable: bool
) -> None:
    error = translated_provider_error(code, "provider detail", status_code=status)
    assert expected in error.details.message
    assert error.details.retryable is retryable
