package com.zvec.lanviewer.wear.ui

import com.zvec.lanviewer.wear.data.PairingAttempt
import com.zvec.lanviewer.wear.data.PairingStatus
import com.zvec.lanviewer.wear.data.RecommendationsResponse
import com.zvec.lanviewer.wear.data.SavedConnection
import com.zvec.lanviewer.wear.data.WatchRepository
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

@OptIn(ExperimentalCoroutinesApi::class)
class WatchViewModelTest {
    @Test
    fun refreshKeepsCurrentSixImagesWhileThePreparedBatchIsDelayed() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val first = CompletableDeferred<RecommendationsResponse>().apply {
                complete(response("ignored", "batch-1"))
            }
            val second = CompletableDeferred<RecommendationsResponse>()
            val repository = FakeRepository(ArrayDeque(listOf(first, second)))
            val viewModel = WatchViewModel(
                repository,
                ThumbnailLoader { true },
                OriginalLoader { true },
                OriginalSaver { },
            )
            advanceUntilIdle()

            assertEquals("batch-1", viewModel.state.value.recommendations.batchId)
            assertEquals(6, viewModel.state.value.recommendations.items.size)
            assertTrue(repository.requestCount >= 2)

            viewModel.loadRecommendations()
            runCurrent()

            assertTrue(viewModel.state.value.recommendations.isLoading)
            assertEquals("batch-1", viewModel.state.value.recommendations.batchId)
            assertEquals(6, viewModel.state.value.recommendations.items.size)

            second.complete(response("ignored", "batch-2"))
            advanceUntilIdle()

            assertEquals("batch-2", viewModel.state.value.recommendations.batchId)
            assertEquals(6, viewModel.state.value.recommendations.items.size)
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun preloadsAllSixOriginalsWithoutOpeningAnItem() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val first = CompletableDeferred<RecommendationsResponse>().apply {
                complete(response("ignored", "batch-1"))
            }
            val next = CompletableDeferred<RecommendationsResponse>()
            val repository = FakeRepository(ArrayDeque(listOf(first, next)))
            val originals = mutableListOf<String>()
            val viewModel = WatchViewModel(
                repository,
                ThumbnailLoader { true },
                OriginalLoader { url -> originals += url; true },
                OriginalSaver { },
            )

            advanceUntilIdle()

            assertEquals((1..6).map { "original-$it" }, originals)
            assertEquals(WatchPage.RECOMMENDATIONS, viewModel.state.value.page)
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun originalViewerStopsAtTheEdgesAndSavesTheSelectedItem() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val first = CompletableDeferred<RecommendationsResponse>().apply {
                complete(response("ignored", "batch-1"))
            }
            val next = CompletableDeferred<RecommendationsResponse>()
            val repository = FakeRepository(ArrayDeque(listOf(first, next)))
            val savedItems = mutableListOf<String>()
            val viewModel = WatchViewModel(
                repository,
                ThumbnailLoader { true },
                OriginalLoader { true },
                OriginalSaver { item -> savedItems += item.itemId },
            )
            advanceUntilIdle()

            viewModel.openOriginal("batch-1-item-1")
            viewModel.showPreviousOriginal()
            assertEquals("batch-1-item-1", viewModel.state.value.selectedItem?.itemId)

            repeat(6) { viewModel.showNextOriginal() }
            assertEquals("batch-1-item-6", viewModel.state.value.selectedItem?.itemId)

            viewModel.saveSelectedOriginal()
            runCurrent()

            assertEquals(listOf("batch-1-item-6"), savedItems)
            assertEquals("已保存到相册", viewModel.state.value.originalSaveMessage)
            assertEquals(listOf("batch-1-item-6"), repository.savedItems)
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun saveFailureDisplaysItsTypeAndConcreteReason() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val first = CompletableDeferred<RecommendationsResponse>().apply {
                complete(response("ignored", "batch-1"))
            }
            val next = CompletableDeferred<RecommendationsResponse>()
            val viewModel = WatchViewModel(
                FakeRepository(ArrayDeque(listOf(first, next))),
                ThumbnailLoader { true },
                OriginalLoader { true },
                OriginalSaver {
                    throw WatchOperationException(
                        WatchErrorType.STORAGE,
                        "手表存储空间不足",
                    )
                },
            )
            advanceUntilIdle()

            viewModel.openOriginal("batch-1-item-1")
            viewModel.saveSelectedOriginal()
            runCurrent()

            assertEquals(
                "保存错误：手表存储空间不足",
                viewModel.state.value.originalSaveMessage,
            )
        } finally {
            Dispatchers.resetMain()
        }
    }
}

private class FakeRepository(
    private val responses: ArrayDeque<CompletableDeferred<RecommendationsResponse>>,
) : WatchRepository {
    var requestCount = 0
        private set
    val savedItems = mutableListOf<String>()

    override fun savedConnection() = SavedConnection("http://39.105.48.52:38522")
    override fun hasToken() = true
    override fun selectConnection(connection: SavedConnection) = Unit
    override fun clearToken() = Unit
    override suspend fun beginPairing(baseUrl: String): PairingAttempt =
        throw UnsupportedOperationException()
    override suspend fun pollPairing(attempt: PairingAttempt): PairingStatus =
        throw UnsupportedOperationException()

    override suspend fun recommendations(requestId: String): RecommendationsResponse {
        requestCount += 1
        val deferred = responses.removeFirstOrNull()
            ?: throw IOException("no queued response")
        return deferred.await().copy(requestId = requestId)
    }

    override suspend fun markShown(batchId: String, eventId: String) = Unit
    override suspend fun recordOpen(batchId: String, itemId: String, eventId: String) = Unit
    override suspend fun recordSave(batchId: String, itemId: String, eventId: String) {
        savedItems += itemId
    }
}
