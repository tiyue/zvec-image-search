package com.zvec.lanviewer.ui

import com.zvec.lanviewer.data.model.SearchItem
import com.zvec.lanviewer.data.model.SearchMode
import kotlinx.collections.immutable.persistentListOf
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
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
}
