package com.zvec.lanviewer.wear.ui

import com.zvec.lanviewer.wear.data.ApiException
import java.io.IOException
import java.net.ConnectException
import java.net.SocketException
import java.net.SocketTimeoutException
import java.net.UnknownHostException

internal enum class WatchErrorType(val label: String) {
    ADDRESS("地址错误"),
    AUTHENTICATION("认证错误"),
    CONNECTION("连接错误"),
    TIMEOUT("超时错误"),
    SERVER("服务端错误"),
    RESPONSE("响应错误"),
    RECOMMENDATION("推荐错误"),
    THUMBNAIL("缩略图错误"),
    ORIGINAL("原图错误"),
    STORAGE("保存错误"),
}

internal enum class WatchOperation {
    CONNECTION,
    RECOMMENDATION,
    THUMBNAIL,
    ORIGINAL,
    SAVE,
}

internal data class WatchFailure(
    val type: WatchErrorType,
    val reason: String,
) {
    val displayText: String
        get() = "${type.label}：$reason"
}

internal class WatchOperationException(
    val type: WatchErrorType,
    message: String,
    cause: Throwable? = null,
) : IOException(message, cause)

internal fun watchFailure(error: Throwable, operation: WatchOperation): WatchFailure {
    error.findCause<WatchOperationException>()?.let { typed ->
        return WatchFailure(typed.type, safeReason(typed.message, fallbackReason(operation)))
    }
    error.findCause<ApiException>()?.let { api ->
        return when (api.httpStatus) {
            401, 403 -> WatchFailure(
                WatchErrorType.AUTHENTICATION,
                "连接授权已失效，请重新配对",
            )
            404 -> WatchFailure(
                WatchErrorType.SERVER,
                "服务器没有对应接口，请确认电脑端源码已更新",
            )
            502 -> WatchFailure(
                WatchErrorType.SERVER,
                "电脑端暂未响应或源码服务未启动",
            )
            in 500..599 -> WatchFailure(
                WatchErrorType.SERVER,
                "HTTP ${api.httpStatus}，${safeReason(api.message, "服务器处理失败")}",
            )
            else -> WatchFailure(
                WatchErrorType.RESPONSE,
                "HTTP ${api.httpStatus}，${safeReason(api.message, "请求未被接受")}",
            )
        }
    }
    if (error.findCause<SocketTimeoutException>() != null) {
        return WatchFailure(WatchErrorType.TIMEOUT, "服务器在规定时间内没有完成响应")
    }
    if (error.findCause<UnknownHostException>() != null) {
        return WatchFailure(WatchErrorType.ADDRESS, "无法解析服务器地址")
    }
    if (error.findCause<ConnectException>() != null) {
        return WatchFailure(WatchErrorType.CONNECTION, "无法连接到填写的 IP 和端口")
    }
    error.findCause<SocketException>()?.let { socket ->
        return WatchFailure(
            WatchErrorType.CONNECTION,
            safeReason(socket.message, "网络连接被中断"),
        )
    }
    if (error.findCause<SecurityException>() != null) {
        return WatchFailure(WatchErrorType.STORAGE, "系统拒绝写入手表相册")
    }
    return WatchFailure(
        type = when (operation) {
            WatchOperation.CONNECTION -> WatchErrorType.CONNECTION
            WatchOperation.RECOMMENDATION -> WatchErrorType.RECOMMENDATION
            WatchOperation.THUMBNAIL -> WatchErrorType.THUMBNAIL
            WatchOperation.ORIGINAL -> WatchErrorType.ORIGINAL
            WatchOperation.SAVE -> WatchErrorType.STORAGE
        },
        reason = safeReason(error.message, fallbackReason(operation)),
    )
}

internal fun watchErrorMessage(error: Throwable, operation: WatchOperation): String =
    watchFailure(error, operation).displayText

private inline fun <reified T : Throwable> Throwable.findCause(): T? {
    var current: Throwable? = this
    repeat(MAX_CAUSE_DEPTH) {
        val candidate = current
        if (candidate is T) return candidate
        val next = candidate?.cause
        if (next === candidate) return null
        current = next
    }
    return null
}

private fun safeReason(raw: String?, fallback: String): String = raw
    ?.replace(CONTROL_CHARACTERS, " ")
    ?.trim()
    ?.takeIf(String::isNotBlank)
    ?.take(MAX_REASON_LENGTH)
    ?: fallback

private fun fallbackReason(operation: WatchOperation): String = when (operation) {
    WatchOperation.CONNECTION -> "无法连接电脑端"
    WatchOperation.RECOMMENDATION -> "无法取得推荐图片"
    WatchOperation.THUMBNAIL -> "无法加载推荐缩略图"
    WatchOperation.ORIGINAL -> "无法加载当前原图"
    WatchOperation.SAVE -> "无法保存当前原图"
}

private const val MAX_CAUSE_DEPTH = 8
private const val MAX_REASON_LENGTH = 120
private val CONTROL_CHARACTERS = Regex("[\\u0000-\\u001f\\u007f]")
