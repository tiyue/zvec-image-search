package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.local.ConnectionMode
import com.zvec.lanviewer.data.local.SavedConnection
import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import com.zvec.lanviewer.data.model.RecommendationItem
import kotlinx.collections.immutable.persistentListOf
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

class AppUiStateTest {
    @Test
    fun resultPagesAppendIntoPersistentListWithoutMutatingPreviousState() {
        val first = SearchItem(mediaId = "m1", name = "one.jpg")
        val second = SearchItem(mediaId = "m2", name = "two.jpg")
        val previous = AppUiState(results = persistentListOf(first))

        val updated = previous.copy(results = previous.results.addAll(listOf(second)))

        assertEquals(listOf(first), previous.results)
        assertEquals(listOf(first, second), updated.results)
        assertSame(first, updated.results.first())
    }

    @Test
    fun pageSizeUsesServerMaximumToReduceRoundTrips() {
        assertEquals(100, SEARCH_PAGE_SIZE)
    }

    @Test
    fun approvedPairingImmediatelyHidesCodeAndShowsLibraryLoadingState() {
        val approved = AppUiState(
            phase = ConnectionPhase.PAIRING,
            pairingCode = "123 456",
            pairingSecondsRemaining = 240L,
            serverName = "Living Room PC",
        ).afterPairingApproved()

        assertEquals(ConnectionPhase.CONNECTING, approved.phase)
        assertNull(approved.pairingCode)
        assertEquals(0L, approved.pairingSecondsRemaining)
        assertEquals("电脑已批准，正在载入图库…", approved.connectionDetail)
        assertEquals("Living Room PC", approved.serverName)
    }

    @Test
    fun semanticSearchModeIsDerivedFromTextAndUploadedImage() {
        assertEquals(SearchMode.TEXT, resolveSearchMode(SearchMode.TEXT, hasText = true, hasImage = false))
        assertEquals(SearchMode.IMAGE, resolveSearchMode(SearchMode.TEXT, hasText = false, hasImage = true))
        assertEquals(SearchMode.COMBINED, resolveSearchMode(SearchMode.TEXT, hasText = true, hasImage = true))
        assertEquals(SearchMode.TAG, resolveSearchMode(SearchMode.TAG, hasText = true, hasImage = true))
    }

    @Test
    fun viewerUsesTheSourceSpecificItems() {
        val search = SearchItem(mediaId = "search")
        val recommendation = RecommendationItem(
            itemId = "item",
            mediaId = "recommended",
            name = "recommended.jpg",
            width = 100,
            height = 100,
            tags = emptyList(),
            libraryId = "library",
            libraryName = "Library",
            contentType = "image/jpeg",
            sizeBytes = 100,
            bucket = "quality",
            thumbnailUrl = "http://127.0.0.1/thumbnail.jpg",
            previewUrl = "http://127.0.0.1/preview.jpg",
        )

        val recommendationState = AppUiState(
            results = persistentListOf(search),
            recommendations = RecommendationUiState(items = persistentListOf(recommendation)),
            viewerSource = ViewerSource.RECOMMENDATIONS,
            viewerIndex = 0,
        )

        assertEquals("recommended", recommendationState.currentViewerItem()?.mediaId)
        assertEquals(listOf(recommendation), recommendationState.viewerItems())
    }

    @Test
    fun modeSwitchKeepsDraftAndClearsStagedResultsAndViewer() {
        val state = AppUiState(
            phase = ConnectionPhase.READY,
            searchText = "sunset",
            searchMode = SearchMode.TAG,
            topKText = "48",
            selectedLibraryIds = setOf("lib-1"),
            results = persistentListOf(SearchItem(mediaId = "m1")),
            totalResults = 1,
            activeSearchId = "search-1",
            viewerSource = ViewerSource.SEARCH,
            viewerIndex = 0,
            errorMessage = "旧错误",
        )

        val switched = state.afterModeSwitched(
            SavedConnection(
                baseUrl = "http://39.105.48.52:38522",
                instanceId = "pc-1",
                displayName = "PC",
                mode = ConnectionMode.REMOTE,
                lanBaseUrl = "http://192.168.1.10:38522",
                remoteBaseUrl = "http://39.105.48.52:38522",
                supportsRemoteMedia = true,
            ),
        )

        assertEquals("sunset", switched.searchText)
        assertEquals(SearchMode.TAG, switched.searchMode)
        assertEquals("48", switched.topKText)
        assertEquals(setOf("lib-1"), switched.selectedLibraryIds)
        assertTrue(switched.results.isEmpty())
        assertEquals(0, switched.totalResults)
        assertNull(switched.activeSearchId)
        assertNull(switched.viewerIndex)
        assertNull(switched.viewerSource)
        assertNull(switched.errorMessage)
        assertEquals(ConnectionPhase.CONNECTING, switched.phase)
        assertEquals(ConnectionMode.REMOTE, switched.connectionMode)
        assertEquals("http://39.105.48.52:38522", switched.connectionOrigin)
        assertEquals(MODE_SWITCH_NOTICE, switched.searchRestoreNotice)
    }
}
