package com.zvec.lanviewer.wear.ui

import com.zvec.lanviewer.wear.data.RecommendationItem
import com.zvec.lanviewer.wear.data.RecommendationsResponse
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.async
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class RecommendationPreloaderTest {
    @Test
    fun anyTwoSuccessfulThumbnailsReleaseTheBatchBeforeTheTailSettles() = runTest {
        val gates = (1..6).associate { index ->
            "thumb-$index" to CompletableDeferred<Boolean>()
        }
        val task = backgroundScope.startRecommendationPreload(
            requestId = "request-1",
            generation = 1,
            criticalCount = 2,
            create = { response(it, "batch-1") },
            preload = { url -> gates.getValue(url).await() },
        )
        val critical = async { task.awaitCritical() }
        val settled = async { task.awaitSettled() }
        runCurrent()

        gates.getValue("thumb-5").complete(true)
        gates.getValue("thumb-6").complete(true)
        runCurrent()

        assertTrue(critical.isCompleted)
        assertEquals("batch-1", critical.await().batchId)
        assertFalse(settled.isCompleted)

        gates.getValue("thumb-1").complete(true)
        gates.getValue("thumb-2").complete(false)
        gates.getValue("thumb-3").complete(true)
        gates.getValue("thumb-4").complete(true)
        advanceUntilIdle()

        assertEquals(1, settled.await().failedCount)
    }
}

internal fun response(requestId: String, batchId: String): RecommendationsResponse =
    RecommendationsResponse(
        requestId = requestId,
        batchId = batchId,
        count = 6,
        items = (1..6).map { index ->
            RecommendationItem(
                itemId = "$batchId-item-$index",
                mediaId = "$batchId-media-$index",
                thumbnailUrl = "thumb-$index",
                previewUrl = "original-$index",
            )
        },
    )
