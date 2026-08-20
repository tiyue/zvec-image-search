package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.network.ApiException
import com.zvec.lanviewer.data.repository.PairingTokenMissingException
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

class PairingDiagnosticsTest {
    @Test
    fun httpFailureIncludesPairingStageStatusAndServerCode() {
        val message = pairingErrorMessage(
            PairingStage.POLL,
            ApiException(401, "invalid_client_secret", "配对凭据无效"),
        )

        assertTrue(message.contains("等待电脑批准"))
        assertTrue(message.contains("HTTP 401"))
        assertTrue(message.contains("invalid_client_secret"))
        assertTrue(message.contains("配对凭据无效"))
    }

    @Test
    fun transportFailureExplainsStageAndRecoveryAction() {
        val message = pairingErrorMessage(PairingStage.CREATE, IOException("connection reset"))

        assertTrue(message.contains("创建配对请求"))
        assertTrue(message.contains("局域网连接中断"))
        assertTrue(message.contains("同一局域网"))
    }

    @Test
    fun approvedWithoutTokenExplainsOldDesktopRecoveryInsteadOfCallingItNetworkLoss() {
        val message = pairingErrorMessage(PairingStage.POLL, PairingTokenMissingException())

        assertTrue(message.contains("等待电脑批准"))
        assertTrue(message.contains("approved_token_missing"))
        assertTrue(message.contains("更新电脑端"))
    }

    @Test
    fun approvedWithoutTokenGetsOneRecoveryPollThenFailsFast() {
        val policy = PairingPollRecoveryPolicy(maxApprovedWithoutTokenResponses = 2)

        assertTrue(policy.shouldRetryApprovedWithoutToken())
        assertFalse(policy.shouldRetryApprovedWithoutToken())

        policy.onSuccessfulStatus()
        assertTrue(policy.shouldRetryApprovedWithoutToken())
    }

    @Test
    fun unexpectedClientFailureKeepsBoundedTypeAndDetailForFieldDiagnosis() {
        val message = pairingErrorMessage(
            PairingStage.CREATE,
            IllegalStateException("unexpected client state\nwith a second line"),
        )

        assertTrue(message.contains("创建配对请求"))
        assertTrue(message.contains("IllegalStateException"))
        assertTrue(message.contains("unexpected client state with a second line"))
        assertFalse(message.contains("发生未知错误"))
        assertFalse(message.contains("\n"))
    }
}
