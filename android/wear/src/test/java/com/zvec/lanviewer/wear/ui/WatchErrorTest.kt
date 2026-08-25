package com.zvec.lanviewer.wear.ui

import com.zvec.lanviewer.wear.data.ApiException
import org.junit.Assert.assertEquals
import org.junit.Test
import java.net.SocketTimeoutException

class WatchErrorTest {
    @Test
    fun networkAndServerFailuresExposeTheirTypeAndReason() {
        assertEquals(
            "超时错误：服务器在规定时间内没有完成响应",
            watchErrorMessage(SocketTimeoutException("timeout"), WatchOperation.RECOMMENDATION),
        )
        assertEquals(
            "服务端错误：服务器没有对应接口，请确认电脑端源码已更新",
            watchErrorMessage(
                ApiException(404, "not_found", "the route was not found"),
                WatchOperation.RECOMMENDATION,
            ),
        )
    }

    @Test
    fun typedSaveFailureKeepsTheConcreteStorageReason() {
        assertEquals(
            "保存错误：手表存储空间不足",
            watchErrorMessage(
                WatchOperationException(
                    WatchErrorType.STORAGE,
                    "手表存储空间不足",
                ),
                WatchOperation.SAVE,
            ),
        )
    }
}
