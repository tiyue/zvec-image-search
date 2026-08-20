package com.zvec.lanviewer.ui

import org.junit.Assert.assertEquals
import org.junit.Test

class PairingDeadlineTest {
    @Test
    fun deadlineUsesMonotonicRelativeDuration() {
        assertEquals(1_300_000L, pairingDeadlineElapsedMillis(1_000_000L, 300L))
    }

    @Test
    fun untrustedServerCannotCreateAnUnboundedPollingDeadline() {
        assertEquals(1_600_000L, pairingDeadlineElapsedMillis(1_000_000L, Long.MAX_VALUE))
        assertEquals(600L, normalizedPairingDurationSeconds(Long.MAX_VALUE))
    }
}
