package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.network.ApiException
import com.zvec.lanviewer.data.repository.PairingTokenMissingException
import java.io.IOException

internal enum class PairingStage(val displayName: String) {
    RESTORE("恢复已保存连接"),
    CREATE("创建配对请求"),
    POLL("等待电脑批准"),
    COMPLETE("完成配对连接"),
}

internal class PairingPollRecoveryPolicy(
    private val maxApprovedWithoutTokenResponses: Int = 2,
) {
    private var approvedWithoutTokenResponses = 0

    init {
        require(maxApprovedWithoutTokenResponses > 0)
    }

    fun onSuccessfulStatus() {
        approvedWithoutTokenResponses = 0
    }

    /** Returns true only while another poll can still recover an interrupted token response. */
    fun shouldRetryApprovedWithoutToken(): Boolean {
        approvedWithoutTokenResponses += 1
        return approvedWithoutTokenResponses < maxApprovedWithoutTokenResponses
    }
}

/** Produces a user-readable error while retaining the exact server diagnostics. */
internal fun pairingErrorMessage(stage: PairingStage, error: Throwable): String = when (error) {
    is ApiException ->
        "${stage.displayName}失败：电脑返回 HTTP ${error.httpStatus}（${error.errorCode}）：${error.message}"
    is PairingTokenMissingException -> "${stage.displayName}失败：${error.message}。"
    is IOException -> {
        val detail = error.message?.trim()?.takeIf(String::isNotEmpty)
        buildString {
            append(stage.displayName)
            append("失败：局域网连接中断")
            if (detail != null) append("（$detail）")
            append("。请确认手机与电脑仍在同一局域网后重试。")
        }
    }
    else -> {
        // Do not erase unexpected client-side failures behind a generic message.  The
        // exception type and bounded message are safe to show locally and make field
        // reports actionable even when USB logcat is unavailable.
        val type = error::class.java.simpleName.takeIf(String::isNotBlank)
            ?: error::class.java.name.substringAfterLast('.')
        val detail = error.message
            ?.replace(Regex("[\\r\\n\\t]+"), " ")
            ?.trim()
            ?.takeIf(String::isNotEmpty)
            ?.take(160)
        buildString {
            append(stage.displayName)
            append("失败：客户端异常（")
            append(type)
            if (detail != null) {
                append("：")
                append(detail)
            }
            append("）。请截图此信息后重试。")
        }
    }
}
