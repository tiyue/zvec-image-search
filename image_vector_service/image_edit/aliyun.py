from __future__ import annotations

import http.client
import json
import os
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlencode, urlsplit, urlunsplit

from PIL import Image, UnidentifiedImageError

from .errors import ImageEditProviderError, translated_provider_error
from .models import ImageEditRequest

_GENERATION_PATH: Final = "/api/v1/services/aigc/multimodal-generation/generation"
_UPLOAD_PATH: Final = "/api/v1/uploads"
_MAX_JSON_RESPONSE_BYTES: Final = 2 * 1024 * 1024
_MAX_RESULT_BYTES: Final = 64 * 1024 * 1024


class ImageEditCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[
    [str, str, Mapping[str, str], bytes | None, float, int], HttpResponse
]


class AliyunImageEditProvider:
    """Synchronous DashScope adapter for one input image and one PNG output."""

    def __init__(
        self,
        *,
        api_key: str,
        api_url: str,
        timeout_seconds: float = 300.0,
        transport: Transport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be non-empty")
        parsed = urlsplit(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("api_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("api_url must not contain credentials")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._api_key = api_key.strip()
        self._api_url = api_url
        self._timeout = float(timeout_seconds)
        self._transport = transport or _http_request

    def edit(
        self,
        source_path: Path,
        request: ImageEditRequest,
        output_path: Path,
        *,
        progress: Callable[[str], None],
        cancel_event: threading.Event,
    ) -> dict[str, object]:
        _check_cancel(cancel_event)
        progress("uploading")
        temporary_url = self._upload(source_path, request.model, cancel_event)

        _check_cancel(cancel_event)
        progress("generating")
        response = self._generate(temporary_url, request)
        request_id = _text(response.get("request_id"))
        result_url = _result_image_url(response)

        _check_cancel(cancel_event)
        progress("downloading")
        result_bytes = self._download_result(result_url)
        _check_cancel(cancel_event)
        _verify_png(result_bytes)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(
            f".{output_path.name}.{secrets.token_hex(8)}.part"
        )
        try:
            with temporary_path.open("xb") as handle:
                handle.write(result_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            _check_cancel(cancel_event)
            os.replace(temporary_path, output_path)
        finally:
            temporary_path.unlink(missing_ok=True)

        usage = response.get("usage")
        usage = usage if isinstance(usage, Mapping) else {}
        return {
            "output_path": str(output_path),
            "output_filename": output_path.name,
            "request_id": request_id,
            "width": _non_negative_int(usage.get("width", usage.get("output_width"))),
            "height": _non_negative_int(
                usage.get("height", usage.get("output_height"))
            ),
            "image_count": _non_negative_int(
                usage.get("image_count", usage.get("output_image_count"))
            )
            or 1,
        }

    def _upload(
        self,
        source_path: Path,
        model: str,
        cancel_event: threading.Event,
    ) -> str:
        policy_query = urlencode({"action": "getPolicy", "model": model})
        policy_url = f"{self._upload_base_url()}?{policy_query}"
        response = self._transport(
            "GET",
            policy_url,
            {
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "yaolens-image-edit/1.0",
            },
            None,
            self._timeout,
            _MAX_JSON_RESPONSE_BYTES,
        )
        policy_body = _json_response(response)
        if response.status != 200:
            _raise_provider_response(response, policy_body)
        data = policy_body.get("data")
        if not isinstance(data, Mapping):
            raise translated_provider_error(
                "invalid_upload_policy", "Upload policy data is missing."
            )
        required = {
            "upload_host",
            "upload_dir",
            "oss_access_key_id",
            "signature",
            "policy",
            "x_oss_object_acl",
            "x_oss_forbid_overwrite",
        }
        if any(not _text(data.get(name)) for name in required):
            raise translated_provider_error(
                "invalid_upload_policy", "Upload policy is incomplete."
            )
        _check_cancel(cancel_event)
        upload_host = _trusted_https_url(_text(data["upload_host"]), "upload host")
        file_name = source_path.name
        key = f"{_text(data['upload_dir']).rstrip('/')}/{file_name}"
        body, content_type = _multipart_body(
            [
                ("OSSAccessKeyId", _text(data["oss_access_key_id"])),
                ("Signature", _text(data["signature"])),
                ("policy", _text(data["policy"])),
                ("x-oss-object-acl", _text(data["x_oss_object_acl"])),
                (
                    "x-oss-forbid-overwrite",
                    _text(data["x_oss_forbid_overwrite"]),
                ),
                ("key", key),
                ("success_action_status", "200"),
            ],
            file_name,
            source_path.read_bytes(),
        )
        upload_response = self._transport(
            "POST",
            upload_host,
            {"Content-Type": content_type, "User-Agent": "yaolens-image-edit/1.0"},
            body,
            self._timeout,
            _MAX_JSON_RESPONSE_BYTES,
        )
        if upload_response.status != 200:
            parsed = _try_json(upload_response.body)
            if not parsed and upload_response.status == 403:
                raise translated_provider_error(
                    "upload_failed",
                    upload_response.body.decode("utf-8", errors="replace"),
                    status_code=upload_response.status,
                )
            _raise_provider_response(upload_response, parsed)
        return f"oss://{key}"

    def _generate(self, image_url: str, request: ImageEditRequest) -> dict[str, object]:
        parameters: dict[str, object] = {
            "n": 1,
            "watermark": False,
        }
        if request.negative_prompt:
            parameters["negative_prompt"] = request.negative_prompt
        if request.size is not None:
            parameters["size"] = request.size
        if request.seed is not None:
            parameters["seed"] = request.seed
        if request.model != "qwen-image-edit":
            parameters["prompt_extend"] = request.prompt_extend
        payload = {
            "model": request.model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"image": image_url},
                            {"text": request.prompt},
                        ],
                    }
                ]
            },
            "parameters": parameters,
        }
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        response = self._transport(
            "POST",
            self._generation_url(),
            {
                "Authorization": f"Bearer {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "X-DashScope-OssResourceResolve": "enable",
                "User-Agent": "yaolens-image-edit/1.0",
            },
            body,
            self._timeout,
            _MAX_JSON_RESPONSE_BYTES,
        )
        decoded = _json_response(response)
        if response.status != 200:
            _raise_provider_response(response, decoded)
        return decoded

    def _download_result(self, url: str) -> bytes:
        trusted = _trusted_https_url(url, "result URL")
        response = self._transport(
            "GET",
            trusted,
            {"Accept": "image/png", "User-Agent": "yaolens-image-edit/1.0"},
            None,
            self._timeout,
            _MAX_RESULT_BYTES,
        )
        if response.status != 200:
            raise translated_provider_error(
                "result_download_failed",
                "The generated image could not be downloaded.",
                status_code=response.status,
            )
        return response.body

    def _generation_url(self) -> str:
        parsed = urlsplit(self._api_url)
        return urlunsplit((parsed.scheme, parsed.netloc, _GENERATION_PATH, "", ""))

    def _upload_base_url(self) -> str:
        parsed = urlsplit(self._api_url)
        host = (parsed.hostname or "").casefold()
        if host.endswith(".ap-southeast-1.maas.aliyuncs.com") or "intl" in host:
            return f"https://dashscope-intl.aliyuncs.com{_UPLOAD_PATH}"
        if host.endswith(".cn-beijing.maas.aliyuncs.com") or host.endswith(
            ".aliyuncs.com"
        ):
            return f"https://dashscope.aliyuncs.com{_UPLOAD_PATH}"
        return urlunsplit((parsed.scheme, parsed.netloc, _UPLOAD_PATH, "", ""))


def _http_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
    maximum: int,
) -> HttpResponse:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OSError("request URL is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise OSError("request URL contains credentials")
    connection_class = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_class(parsed.hostname, parsed.port, timeout=timeout)
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        connection.request(method, target, body=body, headers=dict(headers))
        response = connection.getresponse()
        response_body = response.read(maximum + 1)
        if len(response_body) > maximum:
            raise OSError("response exceeds the safety limit")
        return HttpResponse(
            status=response.status,
            headers={name.casefold(): value for name, value in response.getheaders()},
            body=response_body,
        )
    finally:
        connection.close()


def _json_response(response: HttpResponse) -> dict[str, object]:
    decoded = _try_json(response.body)
    if not decoded and response.status == 200:
        raise translated_provider_error(
            "invalid_response", "DashScope returned an invalid JSON response."
        )
    return decoded


def _try_json(body: bytes) -> dict[str, object]:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _raise_provider_response(
    response: HttpResponse, payload: Mapping[str, object]
) -> None:
    raise translated_provider_error(
        _text(payload.get("code")),
        _text(payload.get("message")) or "DashScope request failed.",
        status_code=response.status,
        request_id=_text(payload.get("request_id")),
    )


def _result_image_url(response: Mapping[str, object]) -> str:
    output = response.get("output")
    if not isinstance(output, Mapping):
        raise translated_provider_error(
            "invalid_response", "Response output is missing."
        )
    choices = output.get("choices")
    if not isinstance(choices, list) or not choices:
        raise translated_provider_error(
            "invalid_response", "Response choices are missing."
        )
    first = choices[0]
    message = first.get("message") if isinstance(first, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        raise translated_provider_error(
            "invalid_response", "Response content is missing."
        )
    for item in content:
        if isinstance(item, Mapping) and _text(item.get("image")):
            return _text(item["image"])
    raise translated_provider_error(
        "invalid_response", "Response image URL is missing."
    )


def _trusted_https_url(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise translated_provider_error(
            "invalid_response", f"DashScope {name} is not a valid HTTPS URL."
        )
    if parsed.username is not None or parsed.password is not None:
        raise translated_provider_error(
            "invalid_response", f"DashScope {name} contains credentials."
        )
    return value


def _multipart_body(
    fields: list[tuple[str, str]],
    file_name: str,
    file_bytes: bytes,
) -> tuple[bytes, str]:
    boundary = f"----YaoLens{secrets.token_hex(16)}"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    safe_name = file_name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                'Content-Disposition: form-data; name="file"; '
                f'filename="{safe_name}"\r\n'
            ).encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _verify_png(content: bytes) -> None:
    from io import BytesIO

    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
            if image.format != "PNG":
                raise ValueError("generated image is not PNG")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise translated_provider_error(
            "invalid_result_image", "Generated result is not a valid PNG image."
        ) from exc


def _check_cancel(event: threading.Event) -> None:
    if event.is_set():
        raise ImageEditCancelled("Image editing was cancelled.")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


__all__ = [
    "AliyunImageEditProvider",
    "HttpResponse",
    "ImageEditCancelled",
    "ImageEditProviderError",
]
