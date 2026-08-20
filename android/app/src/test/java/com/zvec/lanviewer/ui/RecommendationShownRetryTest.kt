package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.network.ApiException
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

@OptIn(ExperimentalCoroutinesApi::class)
class RecommendationShownRetryTest {
    @Test
    fun activeShownJobsOnlyBlockTheirOwnBatches() {
        assertEquals(
            RecommendationShownJobDecision.KEEP_ACTIVE,
            recommendationShownJobDecision(
                requestedBatchId = "batch-1",
                activeBatchIds = setOf("batch-1"),
            ),
        )
        assertEquals(
            RecommendationShownJobDecision.START,
            recommendationShownJobDecision(
                requestedBatchId = "batch-2",
                activeBatchIds = setOf("batch-1"),
            ),
        )
        assertEquals(
            RecommendationShownJobDecision.START,
            recommendationShownJobDecision(
                requestedBatchId = "batch-2",
                activeBatchIds = emptySet(),
            ),
        )
    }

    @Test
    fun retriesTheSameEventUntilShownIsRecorded() = runTest {
        val eventId = "shown-event"
        val submittedEventIds = mutableListOf<String>()

        val recorded = retryRecommendationShown(
            isCurrentBatch = { true },
            submit = {
                submittedEventIds += eventId
                if (submittedEventIds.size < 3) throw IOException("offline")
            },
        )

        assertTrue(recorded)
        assertEquals(listOf(eventId, eventId, eventId), submittedEventIds)
        assertEquals(3_000L, testScheduler.currentTime)
    }

    @Test
    fun doesNotRetryPermanentClientErrors() = runTest {
        var attempts = 0

        val recorded = retryRecommendationShown(
            isCurrentBatch = { true },
            submit = {
                attempts += 1
                throw ApiException(400, "invalid_request", "invalid")
            },
        )

        assertFalse(recorded)
        assertEquals(1, attempts)
        assertEquals(0L, testScheduler.currentTime)
    }
}
