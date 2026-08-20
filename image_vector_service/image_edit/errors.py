from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ImageEditErrorDetails:
    code: str
    message: str
    retryable: bool
    request_id: str = ""
    status_code: int | None = None


class ImageEditProviderError(RuntimeError):
    def __init__(self, details: ImageEditErrorDetails) -> None:
        super().__init__(details.message)
        self.details = details


def translated_provider_error(
    code: str,
    provider_message: str,
    *,
    status_code: int | None = None,
    request_id: str = "",
) -> ImageEditProviderError:
    normalized = code.strip().casefold()
    message_text = provider_message.strip().casefold()

    if normalized == "result_download_failed":
        message = "生成已完成，但结果图片下载失败且尚未保存，请检查网络后重新生成。"
        retryable = True
    elif normalized == "upload_failed":
        message = "阿里云临时上传凭证已失效或拒绝上传，请重新生成。"
        retryable = True
    elif normalized == "invalid_upload_policy":
        message = "阿里云返回的临时上传凭证不完整，请稍后重新生成。"
        retryable = True
    elif normalized == "invalid_response":
        message = "阿里云返回了无法识别的生成结果，请稍后重新生成。"
        retryable = True
    elif normalized == "invalid_result_image":
        message = "阿里云返回的结果不是有效 PNG，文件未保存，请重新生成。"
        retryable = True
    elif normalized in {"invalidapikey", "invalid_api_key"} or status_code == 401:
        message = "DashScope API Key 无效或已失效，请在设置中更新凭证。"
        retryable = False
    elif normalized == "arrearage":
        message = "阿里云账号欠费或余额不足，请检查费用与成本中心。"
        retryable = False
    elif normalized == "allocationquota.freetieronly" or "free tier" in message_text:
        message = "该模型的免费额度已用完，且控制台已开启“免费额度用完即停”。"
        retryable = False
    elif normalized == "endpoint.accessdenied":
        message = (
            "所选模型端点不可用；若为快照模型，它可能已下线，"
            "请改用仍在服务的图片编辑模型。"
        )
        retryable = False
    elif (
        normalized
        in {
            "accessdenied",
            "access_denied",
            "accessdenied.unpurchased",
            "model.accessdenied",
            "workspace.accessdenied",
            "commoditynotpurchased",
        }
        or status_code == 403
    ):
        if "policy expired" in message_text:
            message = "临时上传凭证已过期，请重新生成。"
            retryable = True
        else:
            message = "当前 API Key 无权调用该模型，或该模型尚未开通。"
            retryable = False
    elif normalized in {"modelnotfound", "model_not_found", "model_not_supported"}:
        message = "模型不存在、已下线，或在当前地域不可用。"
        retryable = False
    elif normalized in {"workspacenotfound"}:
        message = "阿里云业务空间不存在，请检查当前 API 地址和凭证所属地域。"
        retryable = False
    elif normalized.startswith("throttling") or status_code == 429:
        message = "请求触发阿里云限流，请降低提交频率后重试。"
        retryable = True
    elif normalized in {"invalidparameter.datainspection"}:
        message = "阿里云无法读取临时上传的图片，请重新生成；若仍失败，请重新导入图片。"
        retryable = True
    elif (
        normalized in {"invalidimageformat", "invalidimage.fileformat"}
        or "decode" in message_text
    ):
        message = "图片格式无效或文件已损坏，请重新导入受支持的图片。"
        retryable = False
    elif normalized in {"invalidimage.imagesize", "invalidimageresolution"}:
        message = "图片分辨率不符合模型要求，请调整图片尺寸后重试。"
        retryable = False
    elif normalized in {"rewritefailed"}:
        message = "提示词智能改写暂时失败，请重试或关闭智能改写。"
        retryable = True
    elif normalized in {
        "modelservingerror",
        "modelunavailable",
        "modelservicefailed",
        "systemerror",
        "internalerror.timeout",
        "requesttimeout",
    } or status_code in {408, 500, 502, 503, 504}:
        message = "阿里云模型服务暂时不可用或响应超时，请稍后重试。"
        retryable = True
    elif status_code is not None and 400 <= status_code < 500:
        message = "模型拒绝了当前图片或参数，请检查图片、尺寸和编辑指令。"
        retryable = False
    else:
        message = "图片生成失败，请稍后重试。"
        retryable = True

    return ImageEditProviderError(
        ImageEditErrorDetails(
            code=code or "image_edit_failed",
            message=message,
            retryable=retryable,
            request_id=request_id,
            status_code=status_code,
        )
    )
